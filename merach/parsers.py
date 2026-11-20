"""
Decoders for the Bluetooth LE characteristics exposed by indoor bikes, plus the calibrated power model.

Supported characteristics:

- FTMS (Fitness Machine Service, 0x1826): Indoor Bike Data 0x2AD2 and its companion characteristics
- Cycling Power (0x1818): CP Measurement 0x2A63
- Cycling Speed and Cadence (0x1816): CSC Measurement 0x2A5B
- Heart Rate (0x180D): HR Measurement 0x2A37
- Device Information (0x180A): text characteristics
- ThinkFIT vendor stream (0xFFF1), reverse-engineered from the Merach S26S head unit

Every decoder takes raw bytes and returns a dict. Keys starting with ``_`` are metadata (flags, raw or
undecoded bytes) and are not plotted; all other numeric keys are metrics.
"""
from __future__ import annotations

import math

BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"

NAMES = {
    # services
    "1800": "Generic Access", "1801": "Generic Attribute", "180a": "Device Information",
    "180d": "Heart Rate", "180f": "Battery", "1816": "Cycling Speed & Cadence",
    "1818": "Cycling Power", "1826": "Fitness Machine",
    "fff0": "Vendor FFF0", "ffe0": "Vendor FFE0", "fee7": "Vendor FEE7",
    # characteristics
    "2a00": "Device Name", "2a01": "Appearance", "2a04": "Conn Params", "2a05": "Service Changed",
    "2a19": "Battery Level", "2a23": "System ID", "2a24": "Model Number", "2a25": "Serial Number",
    "2a26": "Firmware Rev", "2a27": "Hardware Rev", "2a28": "Software Rev", "2a29": "Manufacturer",
    "2a2a": "IEEE Cert", "2a50": "PnP ID",
    "2a37": "HR Measurement", "2a38": "Body Sensor Location",
    "2a5b": "CSC Measurement", "2a5c": "CSC Feature", "2a5d": "Sensor Location",
    "2a55": "SC Control Point", "2a63": "CP Measurement", "2a65": "CP Feature", "2a66": "CP Control Point",
    "2acc": "FTMS Feature", "2ad2": "Indoor Bike Data", "2ad3": "Training Status",
    "2ad4": "Supported Speed Range", "2ad6": "Supported Resistance Range",
    "2ad8": "Supported Power Range", "2ad9": "FTMS Control Point", "2ada": "FTMS Status",
    "fff1": "Vendor FFF1", "fff2": "Vendor FFF2", "ffe1": "Vendor FFE1",
}

# Human-readable metric labels (sent to the dashboard)
FIELD_LABELS = {
    "speed_kmh": "Speed (km/h)", "avg_speed_kmh": "Average speed (km/h)",
    "cadence_rpm": "Cadence (rpm)", "avg_cadence_rpm": "Average cadence (rpm)",
    "distance_m": "Distance (m)", "resistance": "Resistance (bike value)",
    "power_w": "Power (W)", "avg_power_w": "Average power (W)",
    "kcal_total": "Energy (kcal)", "kcal_per_h": "kcal/h", "kcal_per_min": "kcal/min",
    "hr_bpm": "Heart rate (bpm)", "met": "MET", "elapsed_s": "Time (s)",
    "remaining_s": "Remaining time (s)",
}


def short_uuid(uuid: str) -> str:
    """``'00002ad2-0000-1000-8000-00805f9b34fb'`` -> ``'2ad2'``; custom 128-bit UUIDs are returned as-is."""
    u = uuid.lower()
    if u.endswith(BASE_SUFFIX) and u.startswith("0000"):
        return u[4:8]
    return u


def name_for(uuid: str) -> str:
    return NAMES.get(short_uuid(uuid), "")


class _Reader:
    """Little-endian cursor over a byte string."""

    def __init__(self, data: bytes):
        self.b = bytes(data)
        self.i = 0

    def take(self, n: int, signed: bool = False) -> int:
        if self.i + n > len(self.b):
            raise ValueError(f"truncated packet: need {n} bytes at offset {self.i}, length {len(self.b)}")
        v = int.from_bytes(self.b[self.i:self.i + n], "little", signed=signed)
        self.i += n
        return v

    def rest_hex(self) -> str | None:
        return self.b[self.i:].hex() if self.i < len(self.b) else None


# ---------------------------------------------------------------------------------------------- FTMS

def parse_indoor_bike_data(data: bytes) -> dict:
    """FTMS Indoor Bike Data (0x2AD2). Note that flag bit 0 is inverted: 0 means "speed present"."""
    r = _Reader(data)
    flags = r.take(2)
    out: dict = {"_flags": f"0x{flags:04x}"}
    if not flags & 0x0001:
        out["speed_kmh"] = r.take(2) / 100
    if flags & 0x0002:
        out["avg_speed_kmh"] = r.take(2) / 100
    if flags & 0x0004:
        out["cadence_rpm"] = r.take(2) / 2
    if flags & 0x0008:
        out["avg_cadence_rpm"] = r.take(2) / 2
    if flags & 0x0010:
        out["distance_m"] = r.take(3)
    if flags & 0x0020:
        start = r.i
        out["resistance"] = r.take(2, signed=True)
        out["_resistance_raw"] = r.b[start:r.i].hex()
    if flags & 0x0040:
        out["power_w"] = r.take(2, signed=True)
    if flags & 0x0080:
        out["avg_power_w"] = r.take(2, signed=True)
    if flags & 0x0100:
        out["kcal_total"] = r.take(2)
        out["kcal_per_h"] = r.take(2)
        out["kcal_per_min"] = r.take(1)
    if flags & 0x0200:
        out["hr_bpm"] = r.take(1)
    if flags & 0x0400:
        out["met"] = r.take(1) / 10
    if flags & 0x0800:
        out["elapsed_s"] = r.take(2)
    if flags & 0x1000:
        out["remaining_s"] = r.take(2)
    extra = r.rest_hex()
    if extra:
        out["_extra"] = extra
    return out


FTMS_MACHINE_FEATURES = [
    "avg_speed", "cadence", "total_distance", "inclination", "elevation_gain", "pace",
    "step_count", "resistance_level", "stride_count", "expended_energy", "heart_rate",
    "metabolic_equivalent", "elapsed_time", "remaining_time", "power_measurement",
    "force_on_belt_power_output", "user_data_retention",
]
FTMS_TARGET_FEATURES = [
    "speed_target", "inclination_target", "resistance_target", "power_target", "hr_target",
    "expended_energy_target", "step_target", "stride_target", "distance_target",
    "training_time_target", "time_2_hr_zones", "time_3_hr_zones", "time_5_hr_zones",
    "indoor_bike_simulation", "wheel_circumference", "spin_down", "cadence_target",
]


def parse_ftms_feature(data: bytes) -> dict:
    r = _Reader(data)
    mf = r.take(4)
    tf = r.take(4) if len(data) >= 8 else 0
    return {
        "_machine": [n for i, n in enumerate(FTMS_MACHINE_FEATURES) if mf >> i & 1],
        "_target": [n for i, n in enumerate(FTMS_TARGET_FEATURES) if tf >> i & 1],
        "_raw_machine": f"0x{mf:08x}", "_raw_target": f"0x{tf:08x}",
    }


def parse_resistance_range(data: bytes) -> dict:
    r = _Reader(data)
    return {"_min": r.take(2, True), "_max": r.take(2, True), "_increment": r.take(2),
            "_note": "raw values; the spec defines a 0.1 resolution"}


def parse_power_range(data: bytes) -> dict:
    r = _Reader(data)
    return {"_min_w": r.take(2, True), "_max_w": r.take(2, True), "_increment_w": r.take(2)}


def parse_speed_range(data: bytes) -> dict:
    r = _Reader(data)
    return {"_min_kmh": r.take(2) / 100, "_max_kmh": r.take(2) / 100, "_increment_kmh": r.take(2) / 100}


FTMS_STATUS = {
    0x01: "reset", 0x02: "stopped/paused by user", 0x03: "stopped by safety key",
    0x04: "started/resumed by user", 0x05: "target speed changed", 0x06: "target incline changed",
    0x07: "target resistance changed", 0x08: "target power changed", 0x09: "target HR changed",
    0x12: "indoor bike simulation params changed", 0x13: "wheel circumference changed",
    0x14: "spin down status", 0x15: "target cadence changed", 0xFF: "control permission lost",
}


def parse_ftms_status(data: bytes) -> dict:
    op = data[0] if data else None
    return {"_op": FTMS_STATUS.get(op, f"0x{op:02x}" if op is not None else "?"),
            "_param": bytes(data[1:]).hex() or None}


TRAINING_STATUS = {
    0x00: "other", 0x01: "idle", 0x02: "warming up", 0x03: "low intensity interval",
    0x04: "high intensity interval", 0x05: "recovery interval", 0x06: "isometric",
    0x07: "heart rate control", 0x08: "fitness test", 0x09: "speed outside control region (low)",
    0x0A: "speed outside control region (high)", 0x0B: "cool down", 0x0C: "watt control",
    0x0D: "manual mode (quick start)", 0x0E: "pre-workout", 0x0F: "post-workout",
}


def parse_training_status(data: bytes) -> dict:
    if len(data) < 2:
        return {"_raw": bytes(data).hex()}
    return {"_status": TRAINING_STATUS.get(data[1], f"0x{data[1]:02x}")}


CP_RESULTS = {1: "success", 2: "op code not supported", 3: "invalid parameter",
              4: "operation failed", 5: "control not permitted"}


def parse_ftms_control_point(data: bytes) -> dict:
    if len(data) >= 3 and data[0] == 0x80:
        return {"_response_to": f"0x{data[1]:02x}", "_result": CP_RESULTS.get(data[2], data[2])}
    return {"_raw": bytes(data).hex()}


# ------------------------------------------------------------------------ Cycling Power / Speed & Cadence

class _RevCounter:
    """Revolutions per minute from a cumulative counter and an event time (handles roll-over)."""

    def __init__(self, rev_bits: int, time_res: float):
        self.rev_mod = 1 << rev_bits
        self.time_res = time_res
        self.prev: tuple[int, int] | None = None
        self.last_rpm: float | None = None

    def update(self, revs: int, evt_time: int) -> float | None:
        if self.prev is not None:
            dr = (revs - self.prev[0]) % self.rev_mod
            dt = ((evt_time - self.prev[1]) % 65536) / self.time_res
            if dt > 0 and dr < 1000:
                self.last_rpm = dr / dt * 60
            elif dr == 0:
                # no new event: keep the previous value (0 if none yet)
                self.last_rpm = self.last_rpm if self.last_rpm is not None else 0.0
        self.prev = (revs, evt_time)
        return self.last_rpm


class CyclingState:
    """State for counter-based measurements (CSC and Cycling Power)."""

    def __init__(self, wheel_circumference_m: float = 2.105):
        self.circ = wheel_circumference_m
        self.csc_crank = _RevCounter(16, 1024)
        self.csc_wheel = _RevCounter(32, 1024)
        self.cp_crank = _RevCounter(16, 1024)
        self.cp_wheel = _RevCounter(32, 2048)

    def parse_csc(self, data: bytes) -> dict:
        r = _Reader(data)
        flags = r.take(1)
        out: dict = {"_flags": f"0x{flags:02x}"}
        if flags & 0x01:
            revs, t = r.take(4), r.take(2)
            out["_wheel_revs"], out["_wheel_evt"] = revs, t
            rpm = self.csc_wheel.update(revs, t)
            if rpm is not None:
                out["speed_kmh"] = round(rpm * self.circ * 60 / 1000, 2)
        if flags & 0x02:
            revs, t = r.take(2), r.take(2)
            out["_crank_revs"], out["_crank_evt"] = revs, t
            rpm = self.csc_crank.update(revs, t)
            if rpm is not None:
                out["cadence_rpm"] = round(rpm, 1)
        return out

    def parse_cp(self, data: bytes) -> dict:
        r = _Reader(data)
        flags = r.take(2)
        out: dict = {"_flags": f"0x{flags:04x}", "power_w": r.take(2, True)}
        if flags & 0x0001:
            out["_pedal_balance"] = r.take(1) / 2
        if flags & 0x0004:
            out["_accum_torque_nm"] = r.take(2) / 32
        if flags & 0x0010:
            revs, t = r.take(4), r.take(2)
            out["_wheel_revs"] = revs
            rpm = self.cp_wheel.update(revs, t)
            if rpm is not None:
                out["speed_kmh"] = round(rpm * self.circ * 60 / 1000, 2)
        if flags & 0x0020:
            revs, t = r.take(2), r.take(2)
            out["_crank_revs"] = revs
            rpm = self.cp_crank.update(revs, t)
            if rpm is not None:
                out["cadence_rpm"] = round(rpm, 1)
        extra = r.rest_hex()
        if extra:
            out["_extra"] = extra
        return out


class CrankTracker:
    """Per-revolution cadence from an external CSC sensor (cumulative revs + last event time, 1/1024 s).

    Unlike the bike's head unit (whole rpm, averaged over ~3 s), every revolution carries an exact
    timestamp. When no new revolution arrives for a while, cadence drops to zero (sensors keep repeating
    the last value while the crank is stopped).
    """

    def __init__(self):
        self.prev: tuple[int, int] | None = None
        self.rpm = 0.0
        self.period: float | None = None
        self.last_new_wall: float | None = None

    def update(self, revs: int, evt: int, t_wall: float) -> float | None:
        if self.prev is None or (self.last_new_wall is not None and t_wall - self.last_new_wall > 60):
            # first packet, or the sensor slept longer than the 64 s event-time roll-over
            self.prev, self.last_new_wall, self.rpm = (revs, evt), t_wall, 0.0
            return None
        dr = (revs - self.prev[0]) % 65536
        dt = ((evt - self.prev[1]) % 65536) / 1024
        if dr > 0 and dt > 0:
            self.rpm, self.period = dr / dt * 60, dt / dr
            self.prev, self.last_new_wall = (revs, evt), t_wall
            return self.rpm
        return self.stale_check(t_wall)

    def stale_check(self, t_wall: float) -> float:
        if self.last_new_wall is not None and t_wall - self.last_new_wall > max(2.5, 2 * (self.period or 0)):
            self.rpm = 0.0
        return self.rpm


def parse_hr(data: bytes) -> dict:
    r = _Reader(data)
    flags = r.take(1)
    hr = r.take(2) if flags & 0x01 else r.take(1)
    return {"hr_bpm": hr}


def parse_string(data: bytes) -> dict:
    return {"_text": bytes(data).decode("utf-8", errors="replace").strip("\x00 ")}


def parse_battery(data: bytes) -> dict:
    return {"_battery_pct": data[0] if data else None}


# ------------------------------------------------------------------------- ThinkFIT / FitShow (0xFFF1)
# The Merach S26S head unit is a ThinkFIT module ("TFCKIT"). Besides FTMS it streams frames on FFF1:
#     02 | cmd | payload ... | XOR(cmd + payload) | 03
# Layout reverse-engineered by comparing with FTMS (see docs/protocol.md):
#   0x42  live data:     [2] state  [3:5] speed/100  [5] resistance level  [6:8] cadence
#                        [8] ?  [9:11] power/10  [11:13] ?
#   0x43 0x01 totals:    [3:5] time s  [5:7] distance m  [7:9] kcal/10  [9:11] crank revolutions

TF_STATUS = {0x01: "standby", 0x02: "running", 0x03: "paused"}


def parse_thinkfit(data: bytes) -> dict:
    b = bytes(data)
    if len(b) < 4 or b[0] != 0x02 or b[-1] != 0x03:
        return {"_error": "unrecognised frame", "_raw": b.hex()}
    body, chk = b[1:-2], b[-2]
    x = 0
    for v in body:
        x ^= v
    out: dict = {"_cmd": f"0x{b[1]:02x}"}
    if x != chk:
        out["_checksum_error"] = f"expected {x:02x}, got {chk:02x}"
        return out

    def le(i: int, n: int = 2) -> int:
        return int.from_bytes(b[i:i + n], "little")

    if b[1] == 0x42 and len(b) >= 15:
        out["_status"] = TF_STATUS.get(b[2], f"0x{b[2]:02x}")
        out["speed_kmh"] = le(3) / 100
        out["resistance"] = b[5]
        out["cadence_rpm"] = le(6)
        out["power_w"] = le(9) / 10
        out["_unk8"] = b[8]
        out["_unk11_12"] = b[11:13].hex()
    elif b[1] == 0x43 and len(b) >= 13 and b[2] == 0x01:
        out["elapsed_s"] = le(3)
        out["distance_m"] = le(5)
        out["kcal_total"] = le(7) / 10
        out["tf_counter"] = le(9)
    else:
        out["_payload"] = b[2:-2].hex()
    return out


STRING_CHARS = {"2a00", "2a24", "2a25", "2a26", "2a27", "2a28", "2a29"}


class Decoder:
    """Routes each packet to the right decoder based on the characteristic UUID."""

    def __init__(self):
        self.cycling = CyclingState()
        self.table = {
            "2ad2": parse_indoor_bike_data,
            "2acc": parse_ftms_feature,
            "2ad6": parse_resistance_range,
            "2ad8": parse_power_range,
            "2ad4": parse_speed_range,
            "2ada": parse_ftms_status,
            "2ad3": parse_training_status,
            "2ad9": parse_ftms_control_point,
            "2a63": self.cycling.parse_cp,
            "2a5b": self.cycling.parse_csc,
            "2a37": parse_hr,
            "2a19": parse_battery,
            "fff1": parse_thinkfit,
        }

    def decode(self, uuid: str, data: bytes) -> dict | None:
        su = short_uuid(uuid)
        fn = self.table.get(su)
        if fn is None and su in STRING_CHARS:
            fn = parse_string
        if fn is None:
            return None
        try:
            return fn(data)
        except Exception as e:  # malformed packet: report it without stopping the stream
            return {"_error": str(e)}


# ------------------------------------------------------------------------------------ calibrated power
# The bike computes  P = k × cadence, where k comes from a sensor on the resistance knob — but the head
# unit reads that sensor backwards (knob 0–4 → k = 5.03 W/rpm, knob 14 → k = 1.527 W/rpm).
#
# Two models are available:
#   - calibrated (preferred): physical brake model measured with spin-down tests, see tools/spindown.py
#     and calibration.json;
#   - mirrored (fallback, no calibration file): the manufacturer's k table flipped end to end.
DEFAULT_K_MAX = 5.03   # W/rpm reported at knob 0–4 (sensor saturated)
DEFAULT_K_MIN = 1.527  # W/rpm reported at knob 14 (end of travel; at 15 the brake locks the flywheel)

# Reported k → knob position (measured at ~60 rpm). The head unit has 11 discrete k steps between
# knob 4 and knob 14; below 4 the sensor is saturated. Hand-marked positions may differ by ±1 between runs.
KNOB_TABLE = [(5.028, 4), (4.678, 5), (4.328, 6), (3.944, 7), (3.593, 8), (3.243, 9),
              (2.857, 10), (2.472, 11), (2.088, 12), (1.737, 13), (1.527, 14)]

DEFAULT_LOW_KNOB = 2.0  # knob position assumed below 4 when the rider has not marked it


def knob_from_k(k: float) -> float:
    """Estimated knob position from the bike's reported k (4 means "4 or lower")."""
    if k >= KNOB_TABLE[0][0]:
        return float(KNOB_TABLE[0][1])
    if k <= KNOB_TABLE[-1][0]:
        return float(KNOB_TABLE[-1][1])
    for (k1, n1), (k2, n2) in zip(KNOB_TABLE, KNOB_TABLE[1:]):
        if k2 <= k <= k1:
            return round(n1 + (k1 - k) / (k1 - k2) * (n2 - n1), 1)
    return float("nan")


def calibrated_power(cadence_rpm: float, knob: float, cal: dict) -> float:
    """Power from the spin-down model: ``P = I·(a + b0·r^knob·ω)·ω`` with ω the crank speed in rad/s."""
    w = cadence_rpm * math.pi / 30
    b = cal["b0"] * cal["r"] ** knob
    return cal["i_crank"] * (cal["a"] + b * w) * w


def corrected_metrics(m: dict, k_max: float = DEFAULT_K_MAX, k_min: float = DEFAULT_K_MIN,
                      cal: dict | None = None, knob_hint: float | None = None) -> dict:
    """Derive the bike's k, the knob position, a resistance index and the corrected power.

    ``m`` must contain the bike's ``power_w`` and ``cadence_rpm``. With a calibration the power comes from
    the measured physical model; without one, from the mirrored manufacturer table. ``knob_hint`` is the
    position marked by the rider, used only below knob 4 where the bike's sensor cannot tell positions apart.
    """
    p, c = m.get("power_w"), m.get("cadence_rpm")
    if p is None or c is None:
        return {}
    if c <= 0:
        return {"power_corr_w": 0.0}
    if p <= 0:  # transient: cadence updated before power, no estimate
        return {}
    k_bike = p / c
    knob_est = knob_from_k(k_bike)
    pct = (k_max - k_bike) / (k_max - k_min) * 100
    if cal:
        knob = knob_est
        if knob_est <= KNOB_TABLE[0][1]:
            knob = knob_hint if knob_hint is not None and knob_hint <= KNOB_TABLE[0][1] else DEFAULT_LOW_KNOB
        power = calibrated_power(c, knob, cal)
    else:
        knob, power = None, (k_max + k_min - k_bike) * c
    out = {
        "k_bike": round(k_bike, 3),
        "knob_est": knob_est,
        "resist_pct": round(min(100.0, max(0.0, pct)), 1),
        "power_corr_w": round(power, 1),
    }
    if knob is not None:
        out["knob_used"] = knob  # position actually used by the model (≤4: marked by the rider, or 2)
    return out


def metrics_of(decoded: dict | None) -> dict:
    """Only the plottable numeric fields (keys without the ``_`` prefix)."""
    if not decoded:
        return {}
    return {k: v for k, v in decoded.items()
            if not k.startswith("_") and isinstance(v, (int, float)) and not isinstance(v, bool)}
