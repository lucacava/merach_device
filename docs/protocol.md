# Merach S26S Bluetooth protocol notes

Reverse-engineering notes for the S26S head unit, a **ThinkFIT** module ("TFCKIT", firmware 4.0.1.x). All
multi-byte integers are little-endian unless stated otherwise.

## Advertisement

The bike advertises as `MRK-S26S-XXXX`, connectable, with:

| AD type | Value |
|---|---|
| Complete local name | `MRK-S26S-XXXX` |
| 16-bit service UUIDs | `0x1826` (Fitness Machine) |
| Service data | `1826 → 01 20 00`: FTMS flags = machine available, machine type = indoor bike |

The **service data block is required by Garmin devices** to list a device as a smart trainer. A Mac acting as a
peripheral cannot transmit it, which is why the virtual trainer runs on an ESP32 (see
[virtual-trainer.md](virtual-trainer.md)).

The head unit accepts a single connection at a time. Close the Merach app before connecting.

## GATT database

| Service | Characteristic | Properties | Content |
|---|---|---|---|
| `F8C0` (vendor) | `F8C1`…`F8C8` | read | ASCII strings: `ThinkFIT`, `BIKE`, `TFCKIT`, a serial number, `1.0`, `4.0.1`, `MRK-S26S`, `0x0000` |
| `180A` Device Information | `2A27` / `2A26` / `2A28` | read | Hardware `1.0`, firmware `4.0.1.3`, software `4.0.1`; manufacturer, model and serial are empty |
| `FFF0` (vendor) | `FFF1` | notify, indicate | ThinkFIT frames (below), ~10 frames/s |
| | `FFF2` | write | vendor commands (not used) |
| `1826` Fitness Machine | `2ACC` Feature | read | `0x00005687`: avg speed, cadence, distance, resistance level, energy, heart rate, elapsed time, power |
| | `2AD2` Indoor Bike Data | notify | ~1 Hz, alternating packets with flags `0x0075` and `0x0B00` |
| | `2AD3` Training Status | read, notify | `01 0D`: manual mode (quick start) |
| | `2AD6` Resistance Range | read | `10…80`, step `10` (levels 1–8 in tenths) |
| | `2AD8` Power Range | read | `0…1000 W`, step 1 |
| | `2AD9` Control Point | write, indicate | standard FTMS |
| | `2ADA` Status | notify | standard FTMS |
| two custom 128-bit services | — | — | OTA / vendor, not used |

## ThinkFIT frames (`FFF1`)

```
02 | cmd | payload … | XOR(cmd ⊕ payload) | 03
```

The checksum is the XOR of every byte between the start (`02`) and the checksum itself.

### `0x42` — live data (15 bytes)

| Offset | Size | Field |
|---:|---:|---|
| 2 | 1 | State: `01` standby, `02` running, `03` paused |
| 3 | 2 | Speed, 0.01 km/h (= cadence × 0.317) |
| 5 | 1 | Resistance level 1–8 (**inverted**, see below) |
| 6 | 2 | Cadence, rpm (whole numbers, averaged over ~3 s) |
| 8 | 1 | unknown (always 0) |
| 9 | 2 | Power, 0.1 W (= k(level) × cadence) |
| 11 | 2 | unknown (always 0) |

Example: `02 42 02 70 06 06 34 00 00 dd 08 00 00 d1 03` → running, 16.48 km/h, level 6, 52 rpm, 226.9 W.

### `0x43 0x01` — totals (13 bytes)

| Offset | Size | Field |
|---:|---:|---|
| 3 | 2 | Elapsed time, s |
| 5 | 2 | Distance, m |
| 7 | 2 | Energy, 0.1 kcal (= 15 kcal per virtual km) |
| 9 | 2 | **Crank revolutions**, cumulative: +1 per revolution, refreshed ~5 times per second |

The revolution counter gives the time of every crank revolution with ±0.1 s resolution. The spin-down
calibration relies on it.

## How the head unit computes power

Power is always `k × cadence`, where `k` (W per rpm) comes from a discrete table indexed by a sensor on the
resistance knob. There are 11 steps between knob positions 4 and 14:

| Knob | ≤4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| k (W/rpm) | 5.028 | 4.678 | 4.328 | 3.944 | 3.593 | 3.243 | 2.857 | 2.472 | 2.088 | 1.737 | 1.527 |
| Level | 8 | 7–6 | 6 | 5 | 5–4 | 4–3 | 3–2 | 2 | 2–1 | 1 | 1 |

The table runs the wrong way: the hardest knob position gets the smallest `k`. Below position 4 the sensor is
saturated. The connector uses this table backwards to recover the knob position (`merach.parsers.KNOB_TABLE`).

Other derived figures:

- **Speed** = cadence × 0.317 km/h, regardless of resistance.
- **Calories** = 15 kcal per virtual km.
- **Cadence** is computed over fixed ~3 s windows and reported in whole rpm. Stopping makes it drop straight to 0.

## FTMS Indoor Bike Data

Standard FTMS. The head unit splits the fields across two packets:

- flags `0x0075`: cadence, distance, resistance level, power;
- flags `0x0B00`: speed, energy (total / per hour / per minute), heart rate, elapsed time.

The values match the ThinkFIT stream, but power is truncated to whole watts. While the ThinkFIT stream is
flowing, the connector treats it as the primary source and stores overlapping FTMS fields as `ftms_*`.

## Manual (MR-S26)

- The flywheel and pedals are permanently linked: fixed gear, no freewheel.
- Pressing the knob down engages an emergency brake. This is why the flywheel locks past knob position 14.
- The manual does not describe the flywheel construction. Inertia is estimated in
  [calibration.md](calibration.md).
