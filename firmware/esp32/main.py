# Better_Merach on ESP32 (MicroPython): an FTMS smart trainer that re-broadcasts the connector's calibrated data.
#
# Why a separate board: macOS cannot advertise FTMS service data (1826 -> 01 20 00), which Garmin devices
# require to list a trainer. Here the advertisement is built byte by byte, identical to the original bike.
#
# From the Mac (USB serial, one line per second, packets already encoded by the connector):
#   N <indoor_bike_data_hex> <cycling_power_hex>
# To the Mac (every 2 s):
#   S {"fw": ..., "conn": <connected centrals>, "adv": <0/1>, "age": <seconds since the last data line>}
#
# Install: copy this file to the board as main.py (see tools/esp_repl.py).
import bluetooth
import select
import sys
import time
from micropython import const

FW = "Better_Merach-1"
NAME = b"Better_Merach"
MAX_CONN = const(3)

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)

R, W, N, I = bluetooth.FLAG_READ, bluetooth.FLAG_WRITE, bluetooth.FLAG_NOTIFY, bluetooth.FLAG_INDICATE
U = bluetooth.UUID

FTMS = (U(0x1826), (
    (U(0x2ACC), R),          # Fitness Machine Feature
    (U(0x2AD2), N),          # Indoor Bike Data
    (U(0x2AD3), R | N),      # Training Status
    (U(0x2AD6), R),          # Supported Resistance Level Range
    (U(0x2AD8), R),          # Supported Power Range
    (U(0x2AD9), W | I),      # Fitness Machine Control Point
    (U(0x2ADA), N),          # Fitness Machine Status
))
CPS = (U(0x1818), (
    (U(0x2A63), N),          # Cycling Power Measurement
    (U(0x2A65), R),          # Cycling Power Feature
    (U(0x2A5D), R),          # Sensor Location
))
DIS = (U(0x180A), (
    (U(0x2A29), R),          # Manufacturer Name
    (U(0x2A24), R),          # Model Number
))

# Control Point op codes acknowledged (and ignored: resistance is a manual knob)
ACCEPTED = (0x00, 0x01, 0x07, 0x08, 0x11, 0x12, 0x13)


def adv_payload():
    p = bytearray()

    def ad(t, v):
        p.extend(bytes((len(v) + 1, t)))
        p.extend(v)

    ad(0x01, b"\x06")                    # LE general discoverable, BR/EDR not supported
    ad(0x03, b"\x26\x18")                # complete list of 16-bit services: 0x1826 (FTMS)
    ad(0x16, b"\x26\x18\x01\x20\x00")    # FTMS service data: machine available, indoor bike
    ad(0x09, NAME)                       # complete local name
    return bytes(p)


class Trainer:
    def __init__(self):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.ble.config(gap_name=NAME.decode())
        self.ble.irq(self._irq)
        ((self.f_feat, self.f_ibd, self.f_ts, self.f_res, self.f_pwr, self.f_cp, self.f_st),
         (self.c_meas, self.c_feat, self.c_loc),
         (self.d_man, self.d_model)) = self.ble.gatts_register_services((FTMS, CPS, DIS))
        w = self.ble.gatts_write
        w(self.f_feat, b"\x86\x52\x00\x00\x00\x00\x00\x00")   # cadence, distance, resistance, energy, time, power
        w(self.f_ts, b"\x00\x0d")                             # manual mode (quick start)
        w(self.f_res, b"\x00\x00\x8c\x00\x0a\x00")            # knob 0-14 (in tenths), step 1
        w(self.f_pwr, b"\x00\x00\xd0\x07\x01\x00")            # 0-2000 W
        w(self.c_feat, b"\x0c\x00\x00\x00")                   # wheel + crank revolution data
        w(self.c_loc, b"\x0d")                                # rear hub
        w(self.d_man, b"Claude")
        w(self.d_model, b"Merach calibrated")
        self.conns = set()
        self.adv = False
        self.last_data = None
        self.advertise()

    def advertise(self):
        try:
            self.ble.gap_advertise(100_000, adv_data=adv_payload(), connectable=True)
            self.adv = True
        except OSError:
            self.adv = False

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self.conns.add(data[0])
            self.adv = False
            if len(self.conns) < MAX_CONN:
                self.advertise()          # stay visible for a second receiver (e.g. Zwift)
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.conns.discard(data[0])
            self.advertise()
        elif event == _IRQ_GATTS_WRITE:
            conn, handle = data[0], data[1]
            if handle == self.f_cp:
                v = self.ble.gatts_read(self.f_cp)
                if v:
                    op = v[0]
                    res = 0x01 if op in ACCEPTED else 0x02
                    try:
                        self.ble.gatts_indicate(conn, self.f_cp, bytes((0x80, op, res)))
                    except Exception:
                        pass

    def send(self, ibd, cp):
        self.last_data = time.ticks_ms()
        for c in tuple(self.conns):
            try:
                self.ble.gatts_notify(c, self.f_ibd, ibd)
                self.ble.gatts_notify(c, self.c_meas, cp)
            except Exception:
                pass

    def status(self):
        age = -1 if self.last_data is None else time.ticks_diff(time.ticks_ms(), self.last_data) // 1000
        print('S {"fw": "%s", "conn": %d, "adv": %d, "age": %d}' % (FW, len(self.conns), 1 if self.adv else 0, age))


def main():
    t = Trainer()
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    last_status = 0
    while True:
        for _ in poll.ipoll(200):
            line = sys.stdin.readline().strip()
            if line.startswith("N "):
                parts = line.split()
                if len(parts) == 3:
                    try:
                        t.send(bytes.fromhex(parts[1]), bytes.fromhex(parts[2]))
                    except ValueError:
                        pass
        now = time.ticks_ms()
        if time.ticks_diff(now, last_status) > 2000:
            last_status = now
            t.status()


main()
