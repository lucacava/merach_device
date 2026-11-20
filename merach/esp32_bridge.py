"""
USB bridge to the ESP32 board that acts as the "Better_Merach" FTMS trainer (firmware: firmware/esp32/main.py).

The connector computes the calibrated packets (Indoor Bike Data and Cycling Power) and sends them to the
board once per second as text lines ``N <hex> <hex>``. The board re-broadcasts them over Bluetooth and
replies every 2 s with a status line ``S {json}`` (connected centrals, advertising state, data age).
"""
from __future__ import annotations

import glob
import json
import threading
import time

import serial

PORT_PATTERNS = ("/dev/cu.usbserial-*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*")


def find_port() -> str | None:
    """First serial port that looks like a USB-UART bridge (CP210x, CH34x, native USB)."""
    for pattern in PORT_PATTERNS:
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


class Esp32Bridge:
    def __init__(self, log=print):
        self.log = log
        self.ser: serial.Serial | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self.last_status_t = 0.0
        self.status = {"phase": "off", "port": None, "conn": 0, "adv": None, "fw": None, "message": ""}

    @property
    def is_open(self) -> bool:
        return self.ser is not None

    def open(self, port: str | None = None) -> bool:
        port = port or find_port()
        if not port:
            self.status.update(phase="not found", port=None, conn=0, message="Plug the board into a USB port")
            return False
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, 115200, 0.5
            s.dtr = False   # set before opening: avoids resetting the board where the driver allows it
            s.rts = False
            s.open()
        except (serial.SerialException, OSError) as e:
            self.status.update(phase="error", port=port, message=f"port busy or unavailable: {e}")
            return False
        self.ser = s
        self._stop = False
        self.last_status_t = time.time()
        self.status.update(phase="starting", port=port, conn=0, message="")
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self.log(f"ESP32 board: opened {port}")
        return True

    def _reader(self) -> None:
        buf = b""
        while not self._stop and self.ser is not None:
            try:
                chunk = self.ser.read(256)
            except (serial.SerialException, OSError) as e:
                self.status.update(phase="error", message=f"board unplugged? {e}")
                return
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.strip().decode(errors="replace")
                    if text.startswith("S "):
                        try:
                            d = json.loads(text[2:])
                        except ValueError:
                            continue
                        self.last_status_t = time.time()
                        self.status.update(phase="connected", conn=d.get("conn", 0), adv=d.get("adv"),
                                           fw=d.get("fw"), message="")
            elif time.time() - self.last_status_t > 8 and self.status["phase"] in ("connected", "starting"):
                self.status.update(phase="silent", conn=0,
                                   message="The board is not responding: is the firmware installed?")

    def send(self, ibd: bytes, cp: bytes) -> None:
        if self.ser is None:
            return
        try:
            self.ser.write(f"N {ibd.hex()} {cp.hex()}\n".encode())
        except (serial.SerialException, OSError) as e:
            self.status.update(phase="error", message=f"write failed: {e}")

    def close(self) -> None:
        self._stop = True
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = None
        self.status.update(phase="off", conn=0)
