"""
Virtual Bluetooth sensor "Better_Merach": re-broadcasts the calibrated ride data in real time.

It carries the same fields as the bike's own head unit, recomputed with the calibration:

- power: physical brake model from the spin-down tests (not the bike's inverted table)
- calories: from the actual work done (kJ ÷ 4.184 ÷ 24 % gross efficiency), not 15 kcal per virtual km
- speed and distance: "virtual", i.e. what that power would give on a flat road (not cadence × 0.317)
- resistance: the real knob position (0–14), not the bike's inverted level
- cadence and time: measured values

Standard services, recognised without drivers by Garmin Edge, Zwift and similar apps:

- Fitness Machine (0x1826), i.e. a smart trainer: speed, cadence, distance, resistance, power, calories and
  time in a single stream, exactly like the original bike
- Cycling Power (0x1818): power + crank revolutions (cadence) + wheel revolutions (speed)
- Cycling Speed and Cadence (0x1816): for receivers that want speed from a separate sensor

Two outputs are available (setting ``output``):

- ``esp32`` (default): an ESP32 board on USB advertises a real FTMS trainer, byte-for-byte like the bike
  (see ``merach.esp32_bridge`` and ``firmware/esp32/main.py``);
- ``mac``: the Mac itself advertises via CoreBluetooth (``merach.gatt_peripheral``). macOS cannot advertise
  FTMS service data, so Garmin devices only see it as a power or speed/cadence sensor.

Notes: a Mac cannot see its own peripheral (run Zwift on another device); resistance cannot be controlled
(manual knob), so ERG/grade commands are acknowledged and ignored.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

NAME = "Better_Merach"
# 16-bit UUIDs: this is how Garmin and Zwift recognise sensors (see gatt_peripheral.py)
FTMS_SVC, FTMS_FEATURE, INDOOR_BIKE, TRAINING_STATUS = "1826", "2ACC", "2AD2", "2AD3"
RES_RANGE, POWER_RANGE, FTMS_CP, FTMS_STATUS = "2AD6", "2AD8", "2AD9", "2ADA"
CPS_SVC, CP_MEAS, CP_FEATURE, SENSOR_LOCATION = "1818", "2A63", "2A65", "2A5D"
CSC_SVC, CSC_MEAS, CSC_FEATURE = "1816", "2A5B", "2A5C"

WHEEL_CIRCUMFERENCE_M = 2.096   # 700x23; set the same circumference (2096 mm) on receivers using wheel data
SENSOR_LOC_REAR_HUB = 13        # what smart trainers report
FRESH_S = 4.0                   # data older than this means the bike is idle or disconnected
EFFICIENCY = 0.24               # gross efficiency: kcal burned = work / 4.184 / 0.24
MAX_KNOB = 14

DEFAULT_SETTINGS = {"enabled": True, "speed": True, "mass_kg": 109.0,  # 100 kg rider + ~9 kg road bike
                    "profile": "ftms", "output": "esp32"}
OUTPUTS = ("esp32", "mac", "both")
# What the Mac advertises (output "mac"); the GATT database always contains every service
PROFILES = {
    "all": [FTMS_SVC, CPS_SVC, CSC_SVC],      # everything: the receiver picks (Garmin "Search All" picks power)
    "ftms": [FTMS_SVC],                       # trainer only: all data in one sensor
    "cps_csc": [CPS_SVC, CSC_SVC],            # power + speed/cadence, paired separately
    "cps": [CPS_SVC],                         # step-by-step pairing: power only
    "csc": [CSC_SVC],                         # step-by-step pairing: speed/cadence only
}

# FTMS: supported Fitness Machine Features bits and Indoor Bike Data flags
FTMS_MACHINE_FEATURES = (1 << 1) | (1 << 2) | (1 << 7) | (1 << 9) | (1 << 12) | (1 << 14)
#                        cadence   distance   resistance  energy     time        power
IBD_FLAGS = 0x0004 | 0x0010 | 0x0020 | 0x0040 | 0x0100 | 0x0800  # (bit 0 = 0: speed present)
CP_RESULT_OK, CP_RESULT_UNSUPPORTED = 0x01, 0x02
CP_ACCEPTED = {0x00, 0x01, 0x07, 0x08, 0x11, 0x12, 0x13}  # control, reset, start, stop, simulation, wheel


def flat_road_speed(power_w: float, mass_kg: float, crr: float = 0.004, cda: float = 0.32,
                    rho: float = 1.225) -> float:
    """Speed (m/s) on a flat road with no wind: solves P = (Crr·m·g + ½·ρ·CdA·v²)·v by bisection."""
    if power_w <= 0:
        return 0.0
    lo, hi = 0.0, 40.0
    for _ in range(60):
        v = (lo + hi) / 2
        if (crr * mass_kg * 9.81 + 0.5 * rho * cda * v * v) * v < power_w:
            lo = v
        else:
            hi = v
    return (lo + hi) / 2


class RevAccumulator:
    """Cumulative revolutions generated from a rate, with the exact time of the last completed revolution.

    Receivers derive cadence and speed from (revolutions, last event time). Generating them smoothly from the
    measured cadence avoids the ±0.2 s quantisation noise of the bike's revolution counter.
    """

    def __init__(self):
        self.pos = 0.0
        self.t: float | None = None
        self.last_event: float | None = None

    def advance(self, rate_hz: float, now: float) -> None:
        if self.t is None:
            self.t = now
            return
        dt, self.t = now - self.t, now
        if rate_hz <= 0 or dt <= 0 or dt > 5:
            return
        before = int(self.pos)
        self.pos += rate_hz * dt
        if int(self.pos) > before:
            self.last_event = now - (self.pos - int(self.pos)) / rate_hz

    def revs(self) -> int:
        return int(self.pos)

    def event_time(self, epoch: float, ticks_per_s: int = 1024) -> int:
        return int((self.last_event - epoch) * ticks_per_s) % 65536 if self.last_event else 0


def _clamp(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def cp_measurement(power_w: float, crank_revs: int, crank_evt: int,
                   wheel_revs: int | None = None, wheel_evt_2048: int = 0) -> bytes:
    """Cycling Power Measurement: power, [wheel revs + time in 1/2048 s], crank revs + time in 1/1024 s."""
    if wheel_revs is None:
        return struct.pack("<HhHH", 0x0020, _clamp(power_w, -32768, 32767), crank_revs % 65536, crank_evt % 65536)
    return struct.pack("<HhIHHH", 0x0030, _clamp(power_w, -32768, 32767), wheel_revs % 2 ** 32,
                       wheel_evt_2048 % 65536, crank_revs % 65536, crank_evt % 65536)


def csc_measurement(crank_revs: int, crank_evt: int, wheel_revs: int | None = None, wheel_evt: int = 0) -> bytes:
    """CSC Measurement: [wheel revs + time] (speed/distance) + crank revs + time (cadence), 1/1024 s."""
    if wheel_revs is None:
        return struct.pack("<BHH", 0x02, crank_revs % 65536, crank_evt % 65536)
    return struct.pack("<BIHHH", 0x03, wheel_revs % 2 ** 32, wheel_evt % 65536, crank_revs % 65536, crank_evt % 65536)


def indoor_bike_data(speed_kmh: float, cadence_rpm: float, distance_m: float, resistance: float,
                     power_w: float, kcal_total: float, kcal_per_h: float, elapsed_s: float) -> bytes:
    """FTMS Indoor Bike Data, 20 bytes (fits a single notification at the minimum MTU)."""
    dist = _clamp(distance_m, 0, 2 ** 24 - 1)
    return (struct.pack("<HHH", IBD_FLAGS, _clamp(speed_kmh * 100, 0, 65535), _clamp(cadence_rpm * 2, 0, 65535))
            + dist.to_bytes(3, "little")
            + struct.pack("<hhHHBH", _clamp(resistance, -32768, 32767), _clamp(power_w, -32768, 32767),
                          _clamp(kcal_total, 0, 65535), _clamp(kcal_per_h, 0, 65535),
                          _clamp(kcal_per_h / 60, 0, 255), _clamp(elapsed_s, 0, 65535)))


class SensorBroadcaster:
    """Drives both outputs (ESP32 board and/or Mac) and sends one update per second with the latest data."""

    def __init__(self, hub, settings_path: Path, allowed: bool = True, log=print):
        from .esp32_bridge import Esp32Bridge

        self.hub = hub
        self.path = settings_path
        self.allowed = allowed
        self.log = log
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            self.settings.update({k: v for k, v in json.loads(settings_path.read_text()).items()
                                  if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        self.esp = Esp32Bridge(log)
        self.esp_retry_at = 0.0
        self.periph = None          # GattPeripheral, created on first use (needs the Bluetooth permission)
        self.server = None          # not None while the Mac is advertising
        self.running_cfg: tuple | None = None
        self.crank = RevAccumulator()
        self.wheel = RevAccumulator()
        self.epoch = time.time()
        self.distance_m = 0.0
        self.moving_s = 0.0
        self.last_tick: float | None = None
        self.status = {"name": NAME, "phase": "off" if allowed else "unavailable", "clients": 0,
                       "power_w": None, "cadence_rpm": None, "speed_kmh": None, "distance_m": None,
                       "kcal": None, "resistance": None, "sent": 0, "message": "", "mac_on": False,
                       "esp": self.esp.status, **self.settings}

    # ---- settings from the dashboard
    def update_settings(self, data: dict) -> dict:
        if "enabled" in data:
            self.settings["enabled"] = bool(data["enabled"])
        if "speed" in data:
            self.settings["speed"] = bool(data["speed"])
        if data.get("profile") in PROFILES:
            self.settings["profile"] = data["profile"]
        if data.get("output") in OUTPUTS:
            self.settings["output"] = data["output"]
        if "mass_kg" in data:
            try:
                self.settings["mass_kg"] = max(30.0, min(250.0, float(data["mass_kg"])))
            except (TypeError, ValueError):
                pass
        try:
            self.path.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass
        self.status.update(self.settings)
        self.publish()
        return self.status

    def publish(self) -> None:
        self.hub.push({"type": "broadcast", "broadcast": self.status})

    # ---- FTMS control point (Edge/Zwift request control and send simulation parameters)
    def _on_write(self, ch_uuid: str, value: bytes) -> None:
        if ch_uuid != FTMS_CP or not value or self.periph is None:
            return
        op = value[0]
        result = CP_RESULT_OK if op in CP_ACCEPTED else CP_RESULT_UNSUPPORTED  # ERG/resistance: manual knob
        self.periph.notify(FTMS_CP, bytes([0x80, op, result]))
        if op == 0x01:  # reset
            self.distance_m, self.moving_s = 0.0, 0.0

    # ---- Mac output (CoreBluetooth)
    async def _start(self) -> None:
        from .gatt_peripheral import GattPeripheral

        if self.periph is None:
            self.periph = GattPeripheral(asyncio.get_running_loop())
            self.periph.on_write = self._on_write
        speed, profile = self.settings["speed"], self.settings.get("profile", "all")
        services = [
            (FTMS_SVC, [(INDOOR_BIKE, "notify", None),
                        (FTMS_FEATURE, "read", struct.pack("<II", FTMS_MACHINE_FEATURES, 0)),
                        (TRAINING_STATUS, "read", bytes([0x00, 0x0D])),                  # manual mode
                        (RES_RANGE, "read", struct.pack("<hhH", 0, MAX_KNOB * 10, 10)),  # 0–14, step 1
                        (POWER_RANGE, "read", struct.pack("<hhH", 0, 2000, 1)),
                        (FTMS_CP, "write", None),
                        (FTMS_STATUS, "notify", None)]),
            (CPS_SVC, [(CP_MEAS, "notify", None),
                       (CP_FEATURE, "read", struct.pack("<I", 0x0C if speed else 0x08)),  # crank (+ wheel) data
                       (SENSOR_LOCATION, "read", bytes([SENSOR_LOC_REAR_HUB]))]),
            (CSC_SVC, [(CSC_MEAS, "notify", None),
                       (CSC_FEATURE, "read", struct.pack("<H", 0x0003 if speed else 0x0002))]),
        ]
        advertised = PROFILES.get(profile, PROFILES["all"])
        # FTMS advertisements must carry service data "machine available" (0x01) + "indoor bike" (type bit 5);
        # attempted here, but macOS does not transmit it (see gatt_peripheral.py)
        sdata = {FTMS_SVC: bytes([0x01, 0x20, 0x00])} if FTMS_SVC in advertised else None
        name = NAME + {"cps": "_PWR"}.get(profile, "")
        await self.periph.start(name, services, advertise=advertised, service_data=sdata)
        self.server = self.periph
        self.running_cfg = (speed, profile)
        self.log(f"Mac sensor '{name}' advertising as: {profile} {advertised}")
        self.hub.session.event({"t": time.time(), "type": "broadcast", "phase": "start", **self.settings})

    async def _stop(self) -> None:
        if self.server is None:
            return
        try:
            self.periph.stop()
        except Exception as e:  # noqa: BLE001
            self.log(f"Mac sensor: error while stopping: {e}")
        self.server = None
        self.running_cfg = None
        self.log("Mac sensor stopped")
        self.hub.session.event({"t": time.time(), "type": "broadcast", "phase": "stop"})

    def _mac_clients(self) -> int:
        return self.periph.clients if self.periph else 0

    # ---- one update per second
    def _tick(self, mac_on: bool, esp_on: bool) -> None:
        now = time.time()
        dt = min(now - self.last_tick, 5.0) if self.last_tick else 0.0
        self.last_tick = now
        latest = self.hub.latest
        fresh = self.hub.state.get("phase") == "connected" and now - latest.get("_t", 0) < FRESH_S
        power = float(latest.get("power_corr_w") or 0) if fresh else 0.0
        cadence = float(latest.get("cadence_rpm") or 0) if fresh else 0.0
        if cadence <= 0:
            power = 0.0
        knob = latest.get("knob_used", latest.get("knob_est"))
        resistance = float(knob) if knob is not None else 0.0   # like the bike: integer level, range in tenths
        speed_ms = flat_road_speed(power, self.settings["mass_kg"]) if self.settings["speed"] else 0.0
        self.distance_m += speed_ms * dt
        if cadence > 0:
            self.moving_s += dt
        kcal_total = float(latest.get("kcal_est") or 0)
        kcal_per_h = power * 3600 / 4184 / EFFICIENCY

        self.crank.advance(cadence / 60, now)
        self.wheel.advance(speed_ms / WHEEL_CIRCUMFERENCE_M, now)
        crank = (self.crank.revs(), self.crank.event_time(self.epoch, 1024))
        if self.settings["speed"]:
            cp = cp_measurement(power, *crank, self.wheel.revs(), self.wheel.event_time(self.epoch, 2048))
            csc = csc_measurement(*crank, self.wheel.revs(), self.wheel.event_time(self.epoch, 1024))
        else:
            cp, csc = cp_measurement(power, *crank), csc_measurement(*crank)
        ibd = indoor_bike_data(speed_ms * 3.6, cadence, self.distance_m, resistance,
                               power, kcal_total, kcal_per_h, self.moving_s)
        if mac_on:
            self.periph.notify(CP_MEAS, cp)
            self.periph.notify(CSC_MEAS, csc)
            self.periph.notify(INDOOR_BIKE, ibd)
        if esp_on:
            self.esp.send(ibd, cp)
        clients = (self._mac_clients() if mac_on else 0) + ((self.esp.status.get("conn") or 0) if esp_on else 0)
        self.status.update(clients=clients,
                           power_w=round(power), cadence_rpm=round(cadence),
                           speed_kmh=round(speed_ms * 3.6, 1) if self.settings["speed"] else None,
                           distance_m=round(self.distance_m), kcal=round(kcal_total),
                           resistance=knob, sent=self.status["sent"] + 1)

    def _update_phase(self, want: bool, mac_on: bool, esp_on: bool) -> None:
        esp_ok = esp_on and self.esp.status["phase"] == "connected"
        if not want:
            phase = "off"
        elif self.status.get("clients"):
            phase = "connected"
        elif mac_on or esp_ok:
            phase = "advertising"
        else:
            phase = "starting"
        self.status.update(phase=phase, mac_on=mac_on, esp=dict(self.esp.status), message="")

    async def run(self) -> None:
        if not self.allowed:
            self.status.update(phase="unavailable", message="Disabled in simulation mode")
            self.publish()
            return
        while True:
            try:
                want = self.settings["enabled"]
                output = self.settings.get("output", "esp32")
                want_mac = want and output in ("mac", "both")
                want_esp = want and output in ("esp32", "both")
                # Mac output
                cfg = (self.settings["speed"], self.settings.get("profile", "all"))
                if self.server is not None and (not want_mac or cfg != self.running_cfg):
                    await self._stop()
                if want_mac and self.server is None:
                    await self._start()
                # ESP32 output
                now = time.time()
                if self.esp.is_open and (not want_esp or self.esp.status["phase"] == "error"):
                    self.esp.close()
                    self.esp_retry_at = now + 5
                if want_esp and not self.esp.is_open and now >= self.esp_retry_at:
                    if not self.esp.open():
                        self.esp_retry_at = now + 5
                mac_on, esp_on = self.server is not None, self.esp.is_open
                if mac_on or esp_on:
                    self._tick(mac_on, esp_on)
                self._update_phase(want, mac_on, esp_on)
            except asyncio.CancelledError:
                await self._stop()
                self.esp.close()
                raise
            except Exception as e:  # noqa: BLE001
                self.log(f"Virtual sensor: {type(e).__name__}: {e}")
                self.status.update(phase="error", message=f"{type(e).__name__}: {e}")
                self.server = None
                self.publish()
                await asyncio.sleep(4)
                continue
            self.publish()
            await asyncio.sleep(1)
