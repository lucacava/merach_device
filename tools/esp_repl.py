#!/usr/bin/env python3
"""
Minimal MicroPython raw-REPL client for the ESP32 board.

``mpremote`` often fails on boards where DTR/RTS are wired to EN/IO0: opening the port resets the board
halfway through the handshake. This tool waits for the boot to finish before taking control, and retries.

Usage:
    .venv/bin/python tools/esp_repl.py exec "print(1 + 1)"
    .venv/bin/python tools/esp_repl.py put firmware/esp32/main.py main.py   # copy a file to the board
    .venv/bin/python tools/esp_repl.py reset                                # soft reboot (runs main.py)

Options:
    --port /dev/cu.usbserial-XXXX   serial port (default: first USB-UART port found)

The connector holds the serial port while it runs with output "esp32": switch the dashboard output to "Mac"
or stop the connector before using this tool.
"""
from __future__ import annotations

import argparse
import binascii
import glob
import sys
import time

import serial

PORT_PATTERNS = ("/dev/cu.usbserial-*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*", "/dev/cu.usbmodem*")


def find_port() -> str | None:
    for pattern in PORT_PATTERNS:
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


def open_port(port: str) -> serial.Serial:
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.1
    s.dtr = False   # set before opening: no reset pulse where the driver allows it
    s.rts = False
    s.open()
    return s


def read_until(s: serial.Serial, token: bytes, timeout: float = 5.0) -> bytes:
    buf, t = b"", time.time()
    while time.time() - t < timeout:
        buf += s.read(256)
        if token in buf:
            return buf
    raise TimeoutError(f"expected {token!r}, got {buf[-200:]!r}")


def enter_raw(s: serial.Serial, attempts: int = 4) -> None:
    # on macOS opening the port may still reset the board: let the boot finish first
    t, quiet = time.time(), time.time()
    while time.time() - t < 4 and time.time() - quiet < 1.2:
        if s.read(256):
            quiet = time.time()
    for _ in range(attempts):
        s.write(b"\r\x03\x03")      # interrupt a running program (e.g. main.py)
        time.sleep(0.4)
        s.reset_input_buffer()
        s.write(b"\r\x01")          # Ctrl-A: raw REPL
        try:
            read_until(s, b"raw REPL; CTRL-B to exit\r\n>", 2.5)
            return
        except TimeoutError:
            time.sleep(0.5)
    raise TimeoutError("could not enter the raw REPL")


def exec_raw(s: serial.Serial, code: str, timeout: float = 15.0) -> tuple[str, str]:
    """Raw REPL reply: 'OK' + stdout + \\x04 + stderr + \\x04 + '>'."""
    s.write(code.encode() + b"\x04")
    buf, t = b"", time.time()
    while time.time() - t < timeout:
        buf += s.read(512)
        i = buf.find(b"OK")
        if i >= 0 and buf.count(b"\x04", i) >= 2 and buf.rstrip().endswith(b">"):
            out, err = buf[i + 2:].split(b"\x04")[:2]
            return out.decode(errors="replace"), err.decode(errors="replace")
    raise TimeoutError(f"incomplete reply: {buf[-200:]!r}")


def put(s: serial.Serial, local: str, remote: str) -> None:
    with open(local, "rb") as f:
        data = f.read()
    exec_raw(s, f"import binascii\nf = open({remote!r}, 'wb')")
    for i in range(0, len(data), 512):
        chunk = binascii.b2a_base64(data[i:i + 512]).decode().strip()
        _, err = exec_raw(s, f"f.write(binascii.a2b_base64({chunk!r}))")
        if err:
            raise RuntimeError(err)
    exec_raw(s, "f.close()")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_exec = sub.add_parser("exec", help="run Python code on the board")
    p_exec.add_argument("code")
    p_put = sub.add_parser("put", help="copy a local file to the board")
    p_put.add_argument("local")
    p_put.add_argument("remote")
    sub.add_parser("reset", help="soft reboot (runs main.py)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        sys.exit("No USB serial port found: is the board plugged in?")
    s = open_port(port)
    try:
        enter_raw(s)
        if args.cmd == "exec":
            out, err = exec_raw(s, args.code)
            print(out, end="")
            if err:
                print(err, file=sys.stderr)
        elif args.cmd == "put":
            put(s, args.local, args.remote)
            print(f"copied {args.local} → {args.remote}")
        elif args.cmd == "reset":
            s.write(b"\x02")         # leave the raw REPL
            time.sleep(0.2)
            s.write(b"\x04")         # Ctrl-D: soft reboot, runs main.py
            time.sleep(1.5)
            print(s.read(4096).decode(errors="replace"))
    finally:
        s.close()


if __name__ == "__main__":
    main()
