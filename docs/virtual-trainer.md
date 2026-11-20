# Virtual trainer "Better_Merach"

The connector re-broadcasts the calibrated data as a standard Bluetooth smart trainer, so head units and training
apps receive correct power instead of the bike's own figures.

## What is broadcast

One update per second, built in `merach/ble_sensor.py`:

| Service | Characteristic | Content |
|---|---|---|
| Fitness Machine `0x1826` | Indoor Bike Data `0x2AD2` | virtual speed, cadence, distance, resistance (knob position), calibrated power, energy (total / per hour / per minute), moving time — the head unit's fields, recomputed |
| | Control Point `0x2AD9` | request control, start, stop and simulation parameters are acknowledged and ignored; target power/resistance are reported unsupported (manual knob) |
| Cycling Power `0x1818` | CP Measurement `0x2A63` | calibrated power + crank revolutions (cadence) + wheel revolutions (virtual speed) |
| Cycling Speed & Cadence `0x1816` (Mac output) | CSC Measurement `0x2A5B` | wheel + crank revolutions |

- **Virtual speed:** the speed a rider would reach on a flat road at that power, solved from
  `P = (Crr·m·g + ½·ρ·CdA·v²)·v`. Crr is 0.004, CdA 0.32 m², and *m* is the rider + bike weight set in the
  dashboard (default 109 kg). The wheel circumference is 2096 mm.
- **Crank and wheel revolutions** are generated smoothly from the measured cadence and speed. This avoids the
  counter's quantisation noise.
- **When the bike is idle or disconnected**, power and cadence go to zero.

## Outputs

Selected in the dashboard ("Broadcast from") and stored in `settings.json`.

### ESP32 board (recommended)

macOS cannot advertise the FTMS **service data** (`1826 → 01 20 00`) that Garmin devices require to list a trainer.
An ESP32 running MicroPython builds the advertisement byte by byte, identical to the original bike:

```
02 01 06                      flags: LE general discoverable
03 03 26 18                   16-bit services: 0x1826
06 16 26 18 01 20 00          service data: FTMS available, indoor bike
0e 09 "Better_Merach"         complete local name
```

The connector sends the packets over USB serial (`merach/esp32_bridge.py`) and the board notifies them to every
connected central. Up to three connections are accepted, for example an Edge and Zwift at the same time. The
device name (GAP) is also `Better_Merach`.

**Serial protocol** (115200 baud, one line per message):

| Direction | Line |
|---|---|
| Mac → board, 1 Hz | `N <indoor_bike_data_hex> <cycling_power_hex>` |
| board → Mac, every 2 s | `S {"fw": "Better_Merach-1", "conn": 1, "adv": 1, "age": 0}` |

### Mac (CoreBluetooth)

`merach/gatt_peripheral.py` publishes the same services from the Mac. It uses 16-bit UUIDs, because 128-bit ones
overflow the advertisement and get dropped. Limitations:

- **No service data:** Garmin devices list it only as a power sensor or a speed/cadence sensor, and accept one
  type per Bluetooth address. The profile selector offers "power + speed/cadence" and two single-profile steps
  for pairing.
- **The GAP device name is the computer name.**
- **A Mac cannot see its own peripheral:** use another device for Zwift.

## Setting up the ESP32

Any ESP32 with Bluetooth LE works (classic ESP32, S3, C3; **not** the S2). One-time setup:

1. Install the tools: `.venv/bin/pip install -r requirements-dev.txt`.
2. Identify the board, which shows up as `/dev/cu.usbserial-*` or `/dev/cu.usbmodem*`:
   ```bash
   .venv/bin/python -m esptool --port /dev/cu.usbserial-0001 flash_id
   ```
3. Download the MicroPython firmware for your chip from <https://micropython.org/download/> (tested:
   `ESP32_GENERIC-20260824-v1.29.0.bin`). Then flash it:
   ```bash
   .venv/bin/python -m esptool --chip esp32 --port /dev/cu.usbserial-0001 erase_flash
   .venv/bin/python -m esptool --chip esp32 --port /dev/cu.usbserial-0001 --baud 460800 write_flash -z 0x1000 ESP32_GENERIC-*.bin
   ```
   ESP32-S3 and C3 use offset `0x0` instead of `0x1000`.
4. Upload the trainer firmware and reboot:
   ```bash
   .venv/bin/python tools/esp_repl.py put firmware/esp32/main.py main.py
   .venv/bin/python tools/esp_repl.py reset
   ```
   The board prints `S {...}` status lines. `mpremote` often fails on boards that reset when the port opens;
   `esp_repl.py` waits for the boot to finish.
5. Start the connector with the board plugged in. The dashboard shows "ESP32 board: connected on …".

To update the firmware later, set "Broadcast from" to *Mac* or stop the connector, so the serial port is free,
and repeat step 4.

## Pairing

**Garmin Edge** (tested on an Edge 540):

1. Remove any earlier Better_Merach or "MacBook" sensors.
2. Sensors → Add Sensor → Search All → **Better_Merach**. It appears as a trainer.
3. Use an indoor activity profile.

Power, cadence, speed, distance and calories all come from the trainer.

**Zwift / TrainerRoad / similar:** on another computer or tablet, choose Better_Merach for **Power** and
**Cadence**. Selecting it as *Controllable* is harmless, but the resistance does not change: turn the knob
yourself on climbs.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Edge lists Better_Merach only as a power sensor | You are on the Mac output. Switch to the ESP32 board. |
| Dashboard: "ESP32 board: not found" | Board unplugged, or the port name is not `usbserial`/`usbmodem`. |
| Dashboard: "not responding" | `main.py` is not installed on the board: repeat step 4. |
| Connector picked the ESP32 instead of the bike | Should not happen: names starting with `Better_Merach` are excluded. If it does, start with `--name MRK`. |
| Values stay at zero | The bike is idle or not connected: pedal to wake the head unit. |
