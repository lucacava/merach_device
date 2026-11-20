# Power calibration

The S26S head unit reports `power = k × cadence` with `k` read backwards from the knob sensor (see
[protocol.md](protocol.md)). This document describes how the real braking power was measured without a power
meter, and the resulting model in `calibration.json`.

## 1. Establishing the inversion

Two sweeps at constant cadence (~40 and ~60 rpm), moving the knob from 0 to 14 and marking each position in the
dashboard:

| Knob | Reported level | Reported power @ 60 rpm |
|---:|---:|---:|
| 0–4 | 8 | 302–307 W |
| 6 | 6 | 264 W |
| 8 | 5 | 212 W |
| 10 | 3–2 | 169 W |
| 12 | 2–1 | 121 W |
| 14 | 1 | 84 W |

At position 15 the brake locks the flywheel, so 14 is the end of travel. The reported power falls as resistance
rises: the bike's figures are unusable as-is.

## 2. Spin-down method

A magnetic exercise bike dissipates power in two ways:

- **mechanical friction** (bearings, belt): roughly constant torque;
- the **eddy-current brake**: torque proportional to flywheel speed, and growing steeply as the magnets approach
  the flywheel.

With no rider input, the flywheel slows down according to

```
I · dω/dt = −(τ_friction + c(knob) · ω)        →        dω/dt = −(a + b · ω)
```

where ω is the crank angular speed, `I` the inertia seen at the crank, `a = τ_friction / I` and
`b = c(knob) / I`. The power absorbed at a given crank speed is then

```
P(ω) = I · (a + b · ω) · ω
```

### Data source

The ThinkFIT `0x43` frame carries a cumulative **crank revolution counter**, refreshed about five times per
second. During a coast-down it gives the time of every revolution (±0.1 s). No external sensor is needed.

The bike has a fixed gear, so the pedals keep turning with the flywheel. To spin down, either:

- pedal hard, then lift the feet, keeping them clear of the pedals;
- or spin the crank by hand from the side of the bike.

### Fitting

`tools/spindown.py` fits the model to every spin:

1. Spins are split on 6 s pauses. The constant-speed (still driven) section is identified, which gives ω₀.
2. For each spin the **release time** and `b` are searched, with the coast-down solved in closed form:
   `θ(t) = (ω₀ + a/b)(1 − e^(−bt))/b − (a/b)·t`. The crank must not complete another revolution after the last one
   counted.
3. **Friction `a` is shared** by every spin, since bearings and belt do not depend on the knob, and is chosen to
   minimise the total error.
4. The per-spin `b` values are regressed against knob position: `b = b₀ · r^knob`.

Estimating the release time matters most for the strong knob positions, where the flywheel stops within one or
two revolutions.

## 3. Flywheel inertia

The spin-down measures `a` and `b`, which are torques divided by inertia. The absolute scale needs `I`:

- declared flywheel mass: **6 kg**; measured outer diameter: **38 cm**, thickness 3 cm (plastic shell);
- a solid steel disc of that size would weigh about 27 kg, so the mass is mostly in a metal ring under the shell.
  That is typical for eddy-current flywheels, whose magnets act on the rim;
- **I_flywheel ≈ 0.11–0.17 kg·m²** (uniform disc → thin rim); **0.15 kg·m²** is used;
- gear ratio **6:1** (counted), so the inertia at the crank is `I = 0.15 × 6² = 5.4 kg·m²`.

This is the main source of absolute uncertainty: about **±25 %**. A pendulum test and a hanging-weight friction
test were considered but are impractical with this flywheel. An external power meter would remove this
uncertainty.

## 4. Results

One spin per knob position, ~190 rpm starting cadence:

| Knob | Coast revs | b (1/s) | W @ 40 rpm | W @ 60 rpm | W @ 90 rpm |
|---:|---:|---:|---:|---:|---:|
| 0 | 21 | 0.068 | 25 | 42 | 73 |
| 2 | 13 | 0.100 | 28 | 48 | 89 |
| 4 | 11 | 0.147 | 32 | 58 | 111 |
| 6 | 7 | 0.261 | 43 | 83 | 166 |
| 8 | 5 | 0.383 | 54 | 109 | 224 |
| 10 | 3 | 0.681 | 83 | 172 | 368 |
| 12 | 2 | 1.000 | 113 | 240 | 520 |
| 14 | 0 | 1.468 | 157 | 340 | 745 |

Shared friction: **a = 0.80 rad/s²**, i.e. 4.3 N·m at the crank, or about 27 W at 60 rpm.

Brake curve: **b = 0.0640 × 1.258^knob**. The brake strengthens by ×1.26 per knob step, a clean exponential, as
expected from an eddy-current brake whose field decays exponentially with the gap.

Saved model (`calibration.json`):

```json
{"i_crank": 5.4, "a": 0.8, "b0": 0.06404, "r": 1.25809}
```

### Sanity checks

- Knob 0 at 60 rpm ≈ 41 W, the typical floor for a magnetic spin bike. The bike claims 302 W, which would need a
  flywheel of over 30 kg.
- Above knob 10 power climbs steeply (163 → 367 W at 60 rpm), matching the rider's perception.
- Replaying a ramp test gives 43 kJ for 5.5 minutes at an average of 134 W. The head unit claimed 67 kcal.

## 5. Using the model live

For every ThinkFIT sample, `merach.parsers.corrected_metrics`:

1. computes the bike's `k = power / cadence` and maps it to a knob position with `KNOB_TABLE`;
2. below knob 4, where the sensor is saturated, uses the position marked in the dashboard (default 2);
3. computes `P = I · (a + b₀ · r^knob · ω) · ω` with ω from the bike's cadence.

The connector integrates power into work (kJ) and energy: `kcal = kJ / 4.184 / 0.24`, assuming 24 % gross
efficiency, so kcal ≈ kJ.

## 6. Re-calibrating

1. Start the connector. For each knob position (0, 2, 4 … 14):
   - mark it in the dashboard (always for 0–4);
   - spin up as fast as practical and let the flywheel coast to a stop;
   - wait a few seconds.
2. Run the analysis and save the calibration:
   ```bash
   .venv/bin/python tools/spindown.py <session_id> [<session_id> ...] --save
   ```
3. Optional arguments:
   - `--flywheel-inertia` if you have a better inertia estimate;
   - `--ratio` for bikes with a different gear ratio.
4. Restart the connector to load the new `calibration.json`. Recorded sessions are recomputed automatically
   when replayed.

## Limitations

- Absolute scale ±25 % (flywheel inertia); relative accuracy is much better.
- Knob 12–14: very short coast-downs, so mostly extrapolated along the exponential. At 14 the brake also rubs.
- The model assumes eddy torque ∝ speed, which holds well below the brake's critical speed (true for cadences
  up to ~190 rpm, the fitted range).
- Cadence comes from the head unit, averaged over ~3 s. Power during sharp accelerations is smoothed accordingly.
