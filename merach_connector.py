#!/usr/bin/env python3
"""
Merach connector: Bluetooth LE bridge for the Merach S26S head unit and real-time dashboard server.

What it does:

1. scans for BLE devices and picks the bike (FTMS/CPS/CSC service or a "Merach"/"MRK" name)
2. connects, dumps the whole GATT tree and reads every readable characteristic
3. subscribes to EVERY notify/indicate characteristic and logs each raw packet
4. decodes the standard profiles (FTMS, Cycling Power, CSC, HR) and the ThinkFIT vendor stream
5. computes calibrated power, energy and knob position (see merach/parsers.py and calibration.json)
6. serves the dashboard on http://localhost:8765 and streams data over WebSocket
7. re-broadcasts the calibrated data as the "Better_Merach" trainer (ESP32 board or the Mac itself)

Each run writes to sessions/YYYYMMDD-HHMMSS/:

    raw.jsonl    every event (raw packet + decoding, connection changes, knob markers)
    samples.csv  one row per packet with decoded metrics and the last marked knob position
    gatt.json    services, characteristics and values read from the bike
    scan.json    devices seen while scanning, including their full advertisement

On macOS it must be started from Terminal (or with run.command) because it needs the Bluetooth permission.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import WSMsgType, web
from bleak import BleakClient, BleakScanner

from merach import parsers
from merach.ble_sensor import SensorBroadcaster

ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard" / "index.html"
LAST_DEVICE = ROOT / ".last_device"

FTMS_SVC = "00001826-0000-1000-8000-00805f9b34fb"
CPS_SVC = "00001818-0000-1000-8000-00805f9b34fb"
CSC_SVC = "00001816-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE = "00002ad2-0000-1000-8000-00805f9b34fb"
FTMS_CP = "00002ad9-0000-1000-8000-00805f9b34fb"
CSC_MEAS = "00002a5b-0000-1000-8000-00805f9b34fb"
CSC_FEATURE = "00002a5c-0000-1000-8000-00805f9b34fb"
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
SENSOR_HINTS = ("cad", "garmin")
SCAN_LOCK: asyncio.Lock | None = None  # one scan at a time (bike and external sensor)
NAME_HINTS = ("merach", "mrk", "merak")
EFFICIENCY = 0.24  # typical gross cycling efficiency, to convert work into calories burned
FTMS_OVERLAP = {"speed_kmh", "cadence_rpm", "power_w", "resistance", "distance_m", "kcal_total", "elapsed_s"}

CSV_FIELDS = ["t", "elapsed_s_session", "source", "speed_kmh", "cadence_rpm", "power_w", "resistance",
              "distance_m", "kcal_total", "hr_bpm", "avg_speed_kmh", "avg_cadence_rpm", "avg_power_w",
              "elapsed_s", "cad_g_rpm", "power_corr_w", "work_kj", "kcal_est", "knob_used", "resist_pct",
              "knob_est", "k_bike", "tf_counter", "ftms_speed_kmh", "ftms_cadence_rpm", "ftms_power_w",
              "ftms_resistance", "ftms_distance_m", "knob", "raw_hex"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Session:
    """Log files of the current run."""

    def __init__(self, suffix: str = ""):
        self.started = time.time()
        self.dir = ROOT / "sessions" / (datetime.now().strftime("%Y%m%d-%H%M%S") + suffix)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._raw = open(self.dir / "raw.jsonl", "a", buffering=1)
        self._csv_f = open(self.dir / "samples.csv", "a", newline="", buffering=1)
        self._csv = csv.DictWriter(self._csv_f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        self._csv.writeheader()

    def event(self, obj: dict) -> None:
        self._raw.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def sample(self, row: dict) -> None:
        self._csv.writerow(row)

    def write_json(self, name: str, obj) -> None:
        (self.dir / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False))


class MetricDeriver:
    """Turns decoded packets into metrics. The same logic runs live and when replaying a session."""

    def __init__(self, correction: dict, calibration: dict | None = None):
        self.correction = correction
        self.calibration = calibration
        self.knob_hint: float | None = None  # last knob position marked by the rider
        self.tf_seen = 0.0
        self.work_j = 0.0                     # cumulative work from the calibrated power
        self.last_p: tuple[float, float] | None = None
        self.crank = parsers.CrankTracker()

    def derive(self, su: str, decoded: dict | None, t: float) -> dict:
        if su == "2a5b":  # external cadence sensor: per-revolution cadence
            if not decoded or "_crank_revs" not in decoded:
                return {}
            rpm = self.crank.update(decoded["_crank_revs"], decoded["_crank_evt"], t)
            return {} if rpm is None else {"cad_g_rpm": round(rpm, 1)}
        m = parsers.metrics_of(decoded)
        # The ThinkFIT stream (FFF1: faster, power in tenths of a watt) is the primary source while it flows;
        # overlapping FTMS fields are renamed ftms_* so both can be compared.
        if su == "fff1" and m:
            self.tf_seen = t
        elif su == "2ad2" and m and t - self.tf_seen < 5:
            m = {("ftms_" + k if k in FTMS_OVERLAP else k): v for k, v in m.items()}
        if m:
            m.update(parsers.corrected_metrics(m, **self.correction, cal=self.calibration,
                                               knob_hint=self.knob_hint))
            if "power_corr_w" in m:
                self.integrate(m, t)
        return m

    def integrate(self, m: dict, t: float) -> None:
        """Work (kJ) and energy burned (kcal) from the calibrated power, not from the bike's virtual distance.

        kcal = kJ / 4.184 / efficiency; with the typical 24 % gross efficiency, kcal ≈ kJ.
        """
        p = m["power_corr_w"]
        if self.last_p is not None:
            dt = t - self.last_p[0]
            if 0 < dt < 5:  # longer gaps (disconnections) are not counted
                self.work_j += self.last_p[1] * dt
        self.last_p = (t, p)
        kj = self.work_j / 1000
        m["work_kj"] = round(kj, 2)
        m["kcal_est"] = round(kj / (4.184 * EFFICIENCY), 1)


SESSION_ID = re.compile(r"^\d{8}-\d{6}(-sim)?$")


def list_sessions(live_id: str) -> list[dict]:
    out = []
    for d in sorted((ROOT / "sessions").glob("2*"), reverse=True):
        raw = d / "raw.jsonl"
        if SESSION_ID.match(d.name) and raw.exists():
            st = raw.stat()
            out.append({"id": d.name, "size": st.st_size, "mtime": st.st_mtime, "live": d.name == live_id})
    return out


def load_calibration() -> dict | None:
    path = ROOT / "calibration.json"
    try:
        cal = json.loads(path.read_text())
        return cal if all(k in cal for k in ("i_crank", "a", "b0", "r")) else None
    except (OSError, ValueError):
        return None


def load_session(session_id: str, correction: dict, calibration: dict | None = None) -> dict:
    """Re-read raw.jsonl and recompute everything with the current decoders and calibration."""
    if not SESSION_ID.match(session_id):
        raise ValueError("invalid session id")
    raw = ROOT / "sessions" / session_id / "raw.jsonl"
    dec, der = parsers.Decoder(), MetricDeriver(correction, calibration)
    samples, markers, start = [], [], None
    last: dict[str, tuple] = {}  # per source: (t, metrics) of the last sample kept
    with open(raw) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("t")
            if t is None:
                continue
            start = start if start is not None else t
            if r.get("type") == "marker":
                markers.append({"t": t, "knob": r.get("knob"), "note": r.get("note", "")})
                der.knob_hint = r.get("knob")
            elif r.get("type") == "notify" and r.get("hex") is not None:
                m = der.derive(r["char"], dec.decode(r["char"], bytes.fromhex(r["hex"])), t)
                prev = last.get(r["char"])
                if m and not (prev and prev[1] == m and t - prev[0] < 2):  # skip near-duplicate repeats
                    samples.append({"t": t, "src": r["char"], "m": m})
                    last[r["char"]] = (t, m)
    return {"id": session_id, "session_started": start, "samples": samples, "markers": markers,
            "correction": correction, "calibration": calibration}


class Hub:
    """State shared between BLE, log files and WebSocket clients."""

    def __init__(self, session: Session, k_max: float = parsers.DEFAULT_K_MAX,
                 k_min: float = parsers.DEFAULT_K_MIN, calibration: dict | None = None):
        self.session = session
        self.correction = {"k_max": k_max, "k_min": k_min}
        self.calibration = calibration
        self.deriver = MetricDeriver(self.correction, calibration)
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.samples: deque[dict] = deque(maxlen=20000)
        self.markers: list[dict] = []
        self.latest: dict = {}
        self.chars: dict[str, dict] = {}
        self.gatt: dict | None = None
        self.scan: list[dict] = []
        self.knob: float | None = None
        self.state = {"phase": "starting", "device": None, "address": None, "rssi": None,
                      "connected_since": None, "message": ""}
        self.sensor = {"phase": "starting", "device": None, "address": None, "battery": None, "message": ""}
        self.sensor_blacklist: set[str] = set()
        self.sensor_scan: list[dict] = []
        self.packets_total = 0
        self.broadcaster: SensorBroadcaster | None = None

    # ---- WebSocket broadcasting
    def push(self, obj: dict) -> None:
        self.queue.put_nowait(json.dumps(obj, ensure_ascii=False))

    async def broadcaster_task(self) -> None:
        """Send queued messages to every WebSocket client."""
        while True:
            msg = await self.queue.get()
            dead = []
            for ws in list(self.ws_clients):
                try:
                    await ws.send_str(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.ws_clients.discard(ws)

    def set_state(self, **kw) -> None:
        self.state.update(kw)
        self.push({"type": "state", "state": self.state})
        self.session.event({"t": time.time(), "type": "state", **kw})

    def set_sensor(self, **kw) -> None:
        self.sensor.update(kw)
        self.push({"type": "sensor", "sensor": self.sensor})
        self.session.event({"t": time.time(), "type": "sensor", **kw})

    def sensor_tick(self) -> None:
        """External sensors stop sending revolutions when the crank stops: bring cadence down to zero."""
        now = time.time()
        crank = self.deriver.crank
        before = crank.rpm
        if before > 0 and crank.stale_check(now) == 0:
            self.emit("2a5b", {"cad_g_rpm": 0.0}, now)

    # ---- data from BLE
    def on_packet(self, uuid: str, data: bytes, decoded: dict | None, kind: str = "notify") -> None:
        now = time.time()
        su = parsers.short_uuid(uuid)
        hx = bytes(data).hex()
        c = self.chars.setdefault(su, {"uuid": uuid, "name": parsers.name_for(uuid), "count": 0,
                                       "first_t": now})
        c["count"] += 1
        c["last_t"] = now
        c["last_hex"] = hx
        c["last_decoded"] = decoded
        self.packets_total += 1

        self.session.event({"t": now, "type": kind, "char": su, "hex": hx, "decoded": decoded})
        self.push({"type": "raw", "t": now, "char": su, "name": c["name"], "hex": hx,
                   "count": c["count"], "decoded": decoded, "kind": kind})

        m = self.deriver.derive(su, decoded, now)
        if m:
            self.emit(su, m, now, hx, decoded)

    def emit(self, su: str, m: dict, now: float, hx: str = "", decoded: dict | None = None) -> None:
        self.latest.update(m)
        self.latest["_t"] = now
        s = {"t": now, "src": su, "m": m}
        self.samples.append(s)
        self.push({"type": "sample", **s})
        row = {"t": f"{now:.3f}", "elapsed_s_session": f"{now - self.session.started:.2f}",
               "source": su, "knob": self.knob, "raw_hex": hx, **m}
        self.session.sample(row)
        self.print_line(su, m, decoded, hx)

    def print_line(self, su: str, m: dict, decoded: dict | None, hx: str) -> None:
        if su == "2a5b":
            print(f"  2a5b  external cadence={m.get('cad_g_rpm', 0):5.1f} rpm  [{hx}]", flush=True)
            return

        def f(k, fmt):
            return format(m[k], fmt) if k in m else "  -  "
        flags = (decoded or {}).get("_flags", "")
        print(f"  {su}  v={f('speed_kmh', '5.1f')} km/h  rpm={f('cadence_rpm', '5.1f')}  "
              f"P={f('power_w', '6.1f')} W  Pcal={f('power_corr_w', '6.1f')} W  res={f('resistance', '3.0f')}  "
              f"knob={self.knob} (est. {f('knob_est', '4.1f')})  "
              f"[{flags} {hx}]", flush=True)

    def add_marker(self, knob, note: str = "") -> dict:
        try:
            knob = float(knob) if knob not in (None, "") else None
        except (TypeError, ValueError):
            knob = None
        mk = {"t": time.time(), "knob": knob, "note": note or ""}
        self.knob = knob
        self.deriver.knob_hint = knob
        self.markers.append(mk)
        self.session.event({"type": "marker", **mk})
        self.push({"type": "marker", **mk})
        log(f"Knob marker: {knob} {note}")
        return mk

    def status(self) -> dict:
        now = time.time()
        return {
            "state": self.state,
            "sensor": self.sensor,
            "sensor_scan": self.sensor_scan[:30],
            "session_dir": str(self.session.dir),
            "session_started": self.session.started,
            "packets_total": self.packets_total,
            "last_sample_age_s": round(now - self.latest["_t"], 2) if "_t" in self.latest else None,
            "latest": {k: v for k, v in self.latest.items() if not k.startswith("_")},
            "knob": self.knob,
            "correction": self.correction,
            "calibration": self.calibration,
            "broadcast": self.broadcaster.status if self.broadcaster else None,
            "markers": self.markers[-20:],
            "chars": {k: {kk: vv for kk, vv in v.items() if kk != "uuid"} for k, v in self.chars.items()},
            "gatt": self.gatt,
            "scan": self.scan[:25],
        }


# ---------------------------------------------------------------------------------------------- BLE

def score_device(dev, adv, args) -> int:
    """How likely an advertising device is the bike (0 = never)."""
    name = (adv.local_name or dev.name or "").lower()
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    if args.address and dev.address.lower() == args.address.lower():
        return 100
    if args.name and args.name.lower() in name:
        return 90
    if args.address or args.name:
        return 0
    if name.startswith("Better_Merach"):
        return 0  # our own virtual trainer (Mac or ESP32) advertises exactly like the bike: never pick it
    s = 0
    if any(h in name for h in NAME_HINTS):
        s += 50
    if FTMS_SVC in uuids:
        s += 40
    if CPS_SVC in uuids or CSC_SVC in uuids:
        s += 20
    if not args.address and LAST_DEVICE.exists() and LAST_DEVICE.read_text().strip() == dev.address:
        s += 30
    return s


def score_sensor(dev, adv, args, hub: "Hub") -> int:
    """External cadence sensor: CSC service, never the bike, never a rejected device."""
    name = (adv.local_name or dev.name or "").lower()
    uuids = [u.lower() for u in (adv.service_uuids or [])]
    if args.sensor_address:
        return 100 if dev.address.lower() == args.sensor_address.lower() else 0
    if dev.address in hub.sensor_blacklist or dev.address == hub.state.get("address") or FTMS_SVC in uuids:
        return 0
    if args.sensor_name:
        return 90 if args.sensor_name.lower() in name else 0
    # only devices advertising the CSC service: never try watches or other devices
    s = 50 if CSC_SVC in uuids else 0
    if s and any(h in name for h in SENSOR_HINTS):
        s += 20
    return s


async def scan_best(score, timeout: float, threshold: int):
    """Scan until a device reaches the threshold score (or the timeout expires)."""
    seen: dict[str, tuple] = {}

    def cb(dev, adv):
        seen[dev.address] = (dev, adv)

    best = None
    async with SCAN_LOCK:
        scanner = BleakScanner(detection_callback=cb)
        await scanner.start()
        try:
            t_end = time.time() + timeout
            while time.time() < t_end:
                await asyncio.sleep(0.5)
                ranked = sorted(((score(d, a), d, a) for d, a in seen.values()),
                                key=lambda x: (-x[0], -(x[2].rssi or -999)))
                if ranked and ranked[0][0] >= threshold:
                    best = ranked[0]
                    break
        finally:
            await scanner.stop()
    return best, seen


def adv_details(a) -> dict:
    """Everything macOS received in a device's advertisement (used to compare the bike with our sensor)."""
    out = {"service_data": {k: v.hex() for k, v in (a.service_data or {}).items()}, "tx_power": a.tx_power}
    try:
        raw = a.platform_data[1]  # CoreBluetooth advertisement dictionary
        out["raw"] = {str(k): (bytes(v).hex() if hasattr(v, "bytes") else str(v)) for k, v in dict(raw).items()}
    except Exception:  # noqa: BLE001
        pass
    return out


async def find_bike(hub: Hub, args):
    hub.set_state(phase="scanning", message="Looking for the bike… (pedal to wake up the head unit)")
    log("Scanning for BLE devices…")
    best, seen = await scan_best(lambda d, a: score_device(d, a, args), args.scan_timeout, 40)

    hub.scan = [{"address": d.address, "name": a.local_name or d.name, "rssi": a.rssi,
                 "services": a.service_uuids,
                 "manufacturer": {str(k): v.hex() for k, v in a.manufacturer_data.items()},
                 "score": score_device(d, a, args), **adv_details(a)}
                for d, a in sorted(seen.values(), key=lambda x: -(x[1].rssi or -999))]
    hub.session.write_json("scan.json", hub.scan)
    hub.push({"type": "scan", "scan": hub.scan[:25]})
    log(f"Seen {len(seen)} devices.")
    for s in hub.scan[:12]:
        log(f"   {s['rssi']:>4} dBm  {s['address']}  {s['name']!r}  score={s['score']}  {s['services']}")
    if best is None:
        return None
    _, dev, adv = best
    log(f"Selected: {adv.local_name or dev.name!r} ({dev.address}) RSSI {adv.rssi}")
    return dev, adv


async def dump_gatt(client: BleakClient, hub: Hub, decoder: parsers.Decoder) -> dict:
    out = {"services": []}
    for svc in client.services:
        s = {"uuid": svc.uuid, "short": parsers.short_uuid(svc.uuid), "name": parsers.name_for(svc.uuid),
             "characteristics": []}
        for ch in svc.characteristics:
            c = {"uuid": ch.uuid, "short": parsers.short_uuid(ch.uuid), "name": parsers.name_for(ch.uuid),
                 "properties": list(ch.properties), "handle": ch.handle}
            if "read" in ch.properties:
                try:
                    val = await client.read_gatt_char(ch)
                    c["value_hex"] = bytes(val).hex()
                    c["decoded"] = decoder.decode(ch.uuid, val)
                    hub.on_packet(ch.uuid, val, c["decoded"], kind="read")
                except Exception as e:
                    c["read_error"] = str(e)
            s["characteristics"].append(c)
        out["services"].append(s)
    return out


async def ftms_start(client: BleakClient, hub: Hub) -> None:
    """Some bikes only stream Indoor Bike Data after Request Control + Start."""
    for op, label in ((b"\x00", "Request Control"), (b"\x07", "Start/Resume")):
        try:
            await client.write_gatt_char(FTMS_CP, op, response=True)
            log(f"FTMS Control Point: sent {label}")
            hub.session.event({"t": time.time(), "type": "write", "char": "2ad9", "hex": op.hex(), "label": label})
        except Exception as e:
            log(f"FTMS Control Point {label} failed: {e}")
        await asyncio.sleep(0.5)


async def ble_loop(hub: Hub, args) -> None:
    decoder = parsers.Decoder()
    while True:
        try:
            found = await find_bike(hub, args)
            if not found:
                hub.set_state(phase="not found",
                              message="Bike not found: pedal to wake it up and close the Merach app. Retrying…")
                await asyncio.sleep(2)
                continue
            dev, adv = found
            name = adv.local_name or dev.name
            hub.set_state(phase="connecting", device=name, address=dev.address, rssi=adv.rssi,
                          message="Connecting…")
            disconnected = asyncio.Event()
            async with BleakClient(dev, disconnected_callback=lambda _c: disconnected.set(), timeout=25) as client:
                LAST_DEVICE.write_text(dev.address)
                log(f"Connected to {name}. Reading the GATT tree…")
                hub.gatt = await dump_gatt(client, hub, decoder)
                hub.gatt["device"] = {"name": name, "address": dev.address}
                hub.session.write_json("gatt.json", hub.gatt)
                hub.push({"type": "gatt", "gatt": hub.gatt})
                for s in hub.gatt["services"]:
                    log(f"  service {s['short']} {s['name']}")
                    for c in s["characteristics"]:
                        val = f" = {c.get('value_hex')}" if "value_hex" in c else ""
                        log(f"     {c['short']:>8} {c['name']:<28} {','.join(c['properties'])}{val}")

                subscribed = []
                for svc in client.services:
                    for ch in svc.characteristics:
                        if {"notify", "indicate"} & set(ch.properties):
                            uuid = ch.uuid

                            def handler(_sender, data, uuid=uuid):
                                hub.on_packet(uuid, bytes(data), decoder.decode(uuid, data))

                            try:
                                await client.start_notify(ch, handler)
                                subscribed.append(parsers.short_uuid(uuid))
                            except Exception as e:
                                log(f"  cannot subscribe to {parsers.short_uuid(uuid)}: {e}")
                log(f"Subscribed to: {', '.join(subscribed) or 'no characteristics'}")
                hub.set_state(phase="connected", connected_since=time.time(),
                              message=f"Subscribed to {len(subscribed)} characteristics")

                # If FTMS is present but Indoor Bike Data stays silent, request control and start
                if not args.no_control and any(c.uuid.lower() == FTMS_CP for s in client.services for c in s.characteristics):
                    try:
                        await asyncio.wait_for(disconnected.wait(), timeout=6)
                    except asyncio.TimeoutError:
                        pass
                    if not disconnected.is_set() and "2ad2" not in hub.chars:
                        log("No Indoor Bike Data after 6 s: trying Request Control + Start")
                        await ftms_start(client, hub)

                await disconnected.wait()
            log("Disconnected.")
            hub.set_state(phase="disconnected", connected_since=None, message="Disconnected, reconnecting…")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"BLE error: {type(e).__name__}: {e}")
            hub.set_state(phase="error", message=f"{type(e).__name__}: {e}")
        await asyncio.sleep(2)


async def sensor_loop(hub: Hub, args) -> None:
    """Optional external Bluetooth cadence sensor, connected alongside the bike (``--sensor``)."""
    decoder = parsers.Decoder()
    while True:
        try:
            hub.set_sensor(phase="scanning", message="Looking for the cadence sensor… (turn the crank to wake it)")
            best, seen = await scan_best(lambda d, a: score_sensor(d, a, args, hub), 10, 50)
            hub.sensor_scan = [{"address": d.address, "name": a.local_name or d.name, "rssi": a.rssi,
                                "services": a.service_uuids, "score": score_sensor(d, a, args, hub),
                                "manufacturer": {f"0x{k:04x}": v.hex() for k, v in a.manufacturer_data.items()}}
                               for d, a in sorted(seen.values(), key=lambda x: -(x[1].rssi or -999))]
            if best is None:
                hub.set_sensor(phase="not found", message="Cadence sensor not found: turn the crank to wake it")
                await asyncio.sleep(5)
                continue
            _, dev, adv = best
            name = adv.local_name or dev.name or dev.address
            hub.set_sensor(phase="connecting", device=name, address=dev.address, message="Connecting to the sensor…")
            log(f"Sensor: connecting to {name!r} ({dev.address})")
            disconnected = asyncio.Event()
            async with BleakClient(dev, disconnected_callback=lambda _c: disconnected.set(), timeout=20) as client:
                if client.services.get_characteristic(CSC_MEAS) is None:
                    log(f"Sensor {name!r}: no CSC service, ignoring it")
                    hub.sensor_blacklist.add(dev.address)
                    continue
                try:
                    feat = await client.read_gatt_char(CSC_FEATURE)
                    crank_ok = bool(int.from_bytes(bytes(feat[:2]), "little") & 0x02)
                except Exception:
                    crank_ok = True  # feature characteristic missing: try anyway
                if not crank_ok:
                    log(f"Sensor {name!r}: no crank revolution data (speed sensor?), ignoring it")
                    hub.sensor_blacklist.add(dev.address)
                    continue
                battery = None
                try:
                    battery = (await client.read_gatt_char(BATTERY))[0]
                except Exception:
                    pass

                def handler(_sender, data):
                    hub.on_packet(CSC_MEAS, bytes(data), decoder.decode(CSC_MEAS, data))

                await client.start_notify(CSC_MEAS, handler)
                hub.set_sensor(phase="connected", battery=battery, message="")
                log(f"Sensor {name!r} connected (battery {battery}%)")
                while not disconnected.is_set():
                    try:
                        await asyncio.wait_for(disconnected.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        hub.sensor_tick()
            log("Sensor disconnected.")
            hub.set_sensor(phase="disconnected", message="Sensor disconnected, reconnecting…")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"Sensor error: {type(e).__name__}: {e}")
            hub.set_sensor(phase="error", message=f"{type(e).__name__}: {e}")
        await asyncio.sleep(3)


async def simulate_loop(hub: Hub) -> None:
    """Fake bike for UI work: ThinkFIT 0x42 frames with the knob sweeping 0→11 and the inverted reading."""
    import math
    import random
    decoder = parsers.Decoder()
    vendor = "0000fff1-0000-1000-8000-00805f9b34fb"
    hub.set_state(phase="connected", device="SIMULATOR", address="00:00", connected_since=time.time(),
                  message="Simulation mode: fake data")
    hub.set_sensor(phase="connected", device="SIMULATED CADENCE", address="00:01", battery=99, message="")
    t0 = time.time()
    crank_pos, last_evt, loops = 0.0, 0, 0
    while True:
        el = time.time() - t0
        knob = (el / 8) % 12                       # one knob position every 8 s
        k_bike = 5.03 if knob <= 4.5 else 5.03 - 0.39 * (knob - 4.5)
        level = max(1, min(8, round(1 + (k_bike - 2.4) / 0.37)))
        cad = max(0, int(40 + 4 * math.sin(el / 10) + random.uniform(-1, 1)))
        payload = (bytes([0x42, 0x02]) + int(cad * 0.317 * 100).to_bytes(2, "little") + bytes([level])
                   + cad.to_bytes(2, "little") + b"\x00" + int(k_bike * cad * 10).to_bytes(2, "little") + b"\x00\x00")
        chk = 0
        for v in payload:
            chk ^= v
        data = b"\x02" + payload + bytes([chk, 0x03])
        hub.on_packet(vendor, data, decoder.decode(vendor, data))
        # fake external cadence sensor: cumulative revolutions + last event time (1/1024 s)
        before = int(crank_pos)
        crank_pos += cad / 60 * 0.25
        if int(crank_pos) > before:
            last_evt = int(el * 1024) % 65536
        loops += 1
        if loops % 4 == 0:
            csc = bytes([0x02]) + (int(crank_pos) % 65536).to_bytes(2, "little") + last_evt.to_bytes(2, "little")
            hub.on_packet(CSC_MEAS, csc, decoder.decode(CSC_MEAS, csc))
            hub.sensor_tick()
        await asyncio.sleep(0.25)


# ---------------------------------------------------------------------------------------- web server

def make_app(hub: Hub) -> web.Application:
    async def index(_req):
        return web.FileResponse(DASHBOARD, headers={"Cache-Control": "no-store"})

    async def status(_req):
        return web.json_response(hub.status(), dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    async def sessions(_req):
        return web.json_response(list_sessions(hub.session.dir.name))

    async def session(req):
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, load_session, req.match_info["sid"], hub.correction, hub.calibration)
        except (ValueError, FileNotFoundError) as e:
            raise web.HTTPNotFound(text=str(e))
        return web.json_response(data)

    async def broadcast(req):
        if not hub.broadcaster:
            raise web.HTTPServiceUnavailable(text="virtual sensor unavailable")
        return web.json_response(hub.broadcaster.update_settings(await req.json()))

    async def marker(req):
        body = await req.json()
        return web.json_response(hub.add_marker(body.get("knob"), body.get("note", "")))

    async def ws_handler(req):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        await ws.send_str(json.dumps({
            "type": "hello", "session_started": hub.session.started, "state": hub.state, "sensor": hub.sensor,
            "samples": list(hub.samples), "markers": hub.markers, "gatt": hub.gatt,
            "chars": {k: {kk: vv for kk, vv in v.items() if kk != "uuid"} for k, v in hub.chars.items()},
            "scan": hub.scan[:25], "labels": parsers.FIELD_LABELS, "knob": hub.knob,
            "correction": hub.correction, "calibration": hub.calibration,
            "broadcast": hub.broadcaster.status if hub.broadcaster else None,
        }, ensure_ascii=False, default=str))
        hub.ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    if data.get("type") == "marker":
                        hub.add_marker(data.get("knob"), data.get("note", ""))
                    elif data.get("type") == "broadcast" and hub.broadcaster:
                        hub.broadcaster.update_settings(data)
        finally:
            hub.ws_clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/marker", marker)
    app.router.add_post("/api/broadcast", broadcast)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/api/session/{sid}", session)
    app.router.add_get("/ws", ws_handler)
    return app


async def main() -> None:
    ap = argparse.ArgumentParser(description="Merach S26S BLE connector and real-time dashboard")
    ap.add_argument("--address", help="BLE address/UUID of the bike to use")
    ap.add_argument("--name", help="substring of the bike's advertised name")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port (default %(default)s)")
    ap.add_argument("--scan-timeout", type=float, default=15, help="seconds per scan (default %(default)s)")
    ap.add_argument("--no-control", action="store_true", help="never write to the bike's FTMS Control Point")
    ap.add_argument("--simulate", action="store_true", help="fake data, no Bluetooth (UI development)")
    ap.add_argument("--broadcast", action="store_true",
                    help="enable the virtual sensor even in simulation mode (needs the Bluetooth permission)")
    ap.add_argument("--no-calibration", action="store_true",
                    help="ignore calibration.json and use the mirrored manufacturer table")
    ap.add_argument("--sensor", action="store_true",
                    help="also connect an external Bluetooth cadence sensor (CSC service)")
    ap.add_argument("--sensor-address", help="BLE address/UUID of the cadence sensor")
    ap.add_argument("--sensor-name", help="substring of the cadence sensor's name")
    ap.add_argument("--k-max", type=float, default=parsers.DEFAULT_K_MAX,
                    help="W/rpm reported by the bike at minimum knob (default %(default)s)")
    ap.add_argument("--k-min", type=float, default=parsers.DEFAULT_K_MIN,
                    help="W/rpm reported by the bike at maximum knob (default %(default)s)")
    args = ap.parse_args()

    session = Session("-sim" if args.simulate else "")
    cal = None if args.no_calibration else load_calibration()
    hub = Hub(session, k_max=args.k_max, k_min=args.k_min, calibration=cal)
    log(f"Session: {session.dir}")
    if cal:
        log(f"Power model: spin-down calibration (I={cal['i_crank']} kg·m², a={cal['a']}, "
            f"brake ×{cal['r']:.2f} per knob step)")
    else:
        log(f"Power model: mirrored manufacturer table between {args.k_min} and {args.k_max} W/rpm")

    runner = web.AppRunner(make_app(hub))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    log(f"Dashboard: http://localhost:{args.port}   (status: http://localhost:{args.port}/api/status)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    global SCAN_LOCK
    SCAN_LOCK = asyncio.Lock()
    hub.broadcaster = SensorBroadcaster(hub, ROOT / "settings.json",
                                        allowed=not args.simulate or args.broadcast, log=log)
    tasks = [asyncio.create_task(hub.broadcaster_task()), asyncio.create_task(hub.broadcaster.run())]
    if args.simulate:
        tasks.append(asyncio.create_task(simulate_loop(hub)))
    else:
        tasks.append(asyncio.create_task(ble_loop(hub, args)))
        if args.sensor:
            tasks.append(asyncio.create_task(sensor_loop(hub, args)))
        else:
            hub.set_sensor(phase="disabled", message="")
    await stop.wait()
    log("Shutting down…")
    for t in tasks:
        t.cancel()
    # BLE disconnection on macOS sometimes hangs: wait at most 5 s
    _done, pending = await asyncio.wait(tasks, timeout=5)
    if pending:
        log("Slow disconnection: exiting anyway.")
    await runner.cleanup()
    session._raw.flush()
    session._csv_f.flush()
    if pending:
        os._exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
