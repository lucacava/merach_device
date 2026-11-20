#!/usr/bin/env python3
"""
Spin-down calibration from the bike's own crank revolution counter.

Procedure: for each knob position, spin the crank up (by pedalling hard, then lifting the feet, or by hand)
and let the flywheel coast to a stop. The ThinkFIT 0x43 frame carries a cumulative crank revolution counter
(+1 per revolution, refreshed ~5 times per second), which gives the time of every revolution while the
flywheel slows down.

Coast-down model, with ω the crank angular speed:

    dω/dt = -(a + b·ω)

    a  constant friction (bearings, belt) divided by inertia — common to every knob position
    b  eddy-current magnetic brake (torque ∝ speed) divided by inertia — grows with the knob position

Power absorbed at the crank:  P = I · (a + b·ω) · ω,  where I = flywheel inertia × gear ratio².

The fit also estimates the release instant of each spin, so short coast-downs (high knob positions) still
yield a usable value. Across positions the brake follows b = b0 · r^knob, which is saved to calibration.json.

Usage:
    .venv/bin/python tools/spindown.py [SESSION_ID ...] [--flywheel-inertia 0.15] [--ratio 6] [--save]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from merach import parsers  # noqa: E402

GAP_S = 6.0  # pause that separates two spins


def load_spins(raw: Path) -> tuple[list[list[float]], list[tuple[float, float | None]], list[tuple[float, float]]]:
    """Revolution timestamps grouped into spins, knob markers, and the bike's reported k over time."""
    dec = parsers.Decoder()
    revs, marks, ks, last = [], [], [], None
    with open(raw) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "marker":
                marks.append((r["t"], r.get("knob")))
            if r.get("type") != "notify" or r.get("char") != "fff1":
                continue
            x = dec.decode("fff1", bytes.fromhex(r["hex"]))
            if x.get("_cmd") == "0x43" and "tf_counter" in x:
                c = x["tf_counter"]
                if last is not None and c == last + 1:
                    revs.append(r["t"])
                last = c
            elif x.get("_cmd") == "0x42" and x.get("cadence_rpm", 0) > 0 and x.get("power_w", 0) > 0:
                ks.append((r["t"], x["power_w"] / x["cadence_rpm"]))
    spins: list[list[float]] = []
    for t in revs:
        if spins and t - spins[-1][-1] <= GAP_S:
            spins[-1].append(t)
        else:
            spins.append([t])
    return [s for s in spins if len(s) >= 6], marks, ks


def theta(t: float, w0: float, a: float, b: float) -> float:
    """Crank angle travelled t seconds after release (closed-form solution of the model)."""
    if b < 1e-6:
        return w0 * t - 0.5 * a * t * t
    k = w0 + a / b
    return k * (1 - math.exp(-b * t)) / b - (a / b) * t


def t_stop(w0: float, a: float, b: float) -> float:
    """Time until the crank stops."""
    if a <= 0:
        return math.inf
    if b < 1e-6:
        return w0 / a
    return math.log((w0 + a / b) / (a / b)) / b


def coast_theta(dt: float, w0: float, a: float, b: float) -> float:
    """Angle travelled after release; the crank stays put once it has stopped."""
    return theta(min(dt, t_stop(w0, a, b)), w0, a, b)


def split_spin(s: list[float]) -> tuple[list[float], float] | None:
    """Locate the constant-speed section (still driven) that precedes the coast-down.

    Returns (timestamps to fit, constant speed ω0 in rad/s), or None if there is no clear fast section.
    """
    if len(s) < 6:
        return None
    per = [(s[i + 3] - s[i]) / 3 for i in range(len(s) - 3)]
    pmin = min(per)
    # last index whose 3-revolution average is still within 15 % of the fastest revolution
    fast_end = max(i for i, p in enumerate(per) if p <= pmin * 1.15) + 3
    fast_start = max(0, fast_end - 6)
    n_fast = fast_end - fast_start
    if n_fast < 3 or fast_end >= len(s) - 1:
        return None
    w0 = 2 * math.pi * n_fast / (s[fast_end] - s[fast_start])
    return s[fast_start:], w0


def fit_spin(ts: list[float], w0: float, a: float) -> tuple[float, float, float]:
    """With friction ``a`` fixed, search the release time t_r and brake b. Returns (error, b, t_r)."""
    th = [i * 2 * math.pi for i in range(len(ts))]
    best = (math.inf, 0.0, 0.0)

    def lin(lo: float, hi: float, n: int) -> list[float]:
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    for b in [0.0] + [10 ** (x / 12) for x in range(-30, 7)]:          # 0.003 … 3.2  1/s
        tstop = t_stop(w0, a, b)
        for t_r in lin(ts[0], ts[-1], 41):
            f = [(-w0 * (t_r - t)) if t <= t_r else coast_theta(t - t_r, w0, a, b) for t in ts]
            off = sum(y - m for y, m in zip(th, f)) / len(ts)
            e = sum((y - m - off) ** 2 for y, m in zip(th, f))
            # after the last counted revolution the crank must not complete another one
            final = off + coast_theta(tstop, w0, a, b) if tstop < math.inf else math.inf
            if final - th[-1] >= 2 * math.pi or final < th[-1]:
                continue
            if e < best[0]:
                best = (e, b, t_r)
    return best


def analyze(session: str, i_crank: float, a_fixed: float | None = None) -> tuple[list[dict], float]:
    raw = ROOT / "sessions" / session / "raw.jsonl"
    spins, marks, ks = load_spins(raw)
    items = []
    for s in spins:
        mark = None
        for tm, kn in marks:
            if tm <= s[-1]:
                mark = kn
        kk = [k for t, k in ks if s[0] - 3 <= t <= s[-1] + 3]
        est = parsers.knob_from_k(st.median(kk)) if kk else None
        items.append((s, mark, est, split_spin(s)))
    usable = [sp for _s, _m, _e, sp in items if sp]
    # friction shared by every spin: pick the a that minimises the total error
    if a_fixed is None:
        candidates = [x * 0.1 for x in range(0, 41)]
        a_fixed = min(candidates, key=lambda a: sum(fit_spin(sp[0], sp[1], a)[0] for sp in usable))
    out = []
    for s, mark, est, sp in items:
        row = {"session": session, "t": s[0], "revs": len(s), "knob_marked": mark, "knob_est": est}
        if not sp:
            row["note"] = "no recognisable driven/coast sections"
            out.append(row)
            continue
        ts, w0 = sp
        e, b, t_r = fit_spin(ts, w0, a_fixed)
        coast_revs = sum(1 for t in ts if t > t_r)
        row.update({"a": round(a_fixed, 3), "b": round(b, 4), "start_rpm": round(w0 * 30 / math.pi),
                    "coast_revs": coast_revs, "rms_revs": round(math.sqrt(e / len(ts)) / (2 * math.pi), 3)})
        if coast_revs < 2:
            row["note"] = "coast-down too short: very uncertain estimate"
        for rpm in (40, 60, 90):
            w = rpm * math.pi / 30
            row[f"P_{rpm}rpm_W"] = round(i_crank * (a_fixed + b * w) * w, 1)
        out.append(row)
    return out, a_fixed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="*", help="session IDs (default: the latest)")
    ap.add_argument("--flywheel-inertia", type=float, default=0.15,
                    help="flywheel inertia in kg·m² (default 0.15: 6 kg, Ø 38 cm, rim-weighted)")
    ap.add_argument("--ratio", type=float, default=6.0, help="flywheel turns per crank turn (default 6)")
    ap.add_argument("--save", action="store_true", help="write the calibration to calibration.json")
    args = ap.parse_args()
    sessions = args.sessions or [sorted(p.name for p in (ROOT / "sessions").glob("2*"))[-1]]
    i_crank = args.flywheel_inertia * args.ratio ** 2
    print(f"Inertia at the crank: {i_crank:.2f} kg·m²  (flywheel {args.flywheel_inertia} × {args.ratio}²)\n")
    print(" session          knob   coast revs  a      b       rms    W@40  W@60  W@90")
    allrows = []
    for sid in sessions:
        rows, a = analyze(sid, i_crank)
        print(f" (shared friction: a = {a:.2f} rad/s² → {a * i_crank:.1f} N·m at the crank)")
        allrows += rows
        for r in rows:
            knob = r.get("knob_marked") if r.get("knob_marked") is not None else r.get("knob_est")
            knob_s = "?" if knob is None else ("≤4" if r.get("knob_marked") is None and knob <= 4 else f"{knob:g}")
            if "a" not in r:
                print(f" {sid}  {knob_s:>6}      -        {r['note']}")
                continue
            print(f" {sid}  {knob_s:>6}  {r['coast_revs']:>5}      {r['a']:5.2f}  {r['b']:6.3f}  {r['rms_revs']:5.2f}  "
                  f"{r['P_40rpm_W']:5.0f} {r['P_60rpm_W']:5.0f} {r['P_90rpm_W']:5.0f}  {r.get('note', '')}")
        (ROOT / "sessions" / sid / "spindown.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print("\nPer-spin results written to sessions/<id>/spindown.json")

    # brake curve: b(knob) = b0 · r^knob, log-linear regression over spins with ≥ 2 coast revolutions
    pts = []
    for r in allrows:
        knob = r.get("knob_marked") if r.get("knob_marked") is not None else r.get("knob_est")
        if knob is not None and r.get("b", 0) > 0 and r.get("coast_revs", 0) >= 2:
            pts.append((float(knob), math.log(r["b"])))
    if len(pts) < 3:
        print("Not enough valid spins to fit the brake curve.")
        return
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
    b0, r = math.exp(my - slope * mx), math.exp(slope)
    a = allrows[0].get("a") or 0.0
    print(f"\nBrake curve: b = {b0:.4f} × {r:.3f}^knob   (×{r:.2f} per knob step, {n} spins)")
    print(" knob   W@40  W@60  W@90  (model)")
    for knob in range(0, 15, 2):
        bb = b0 * r ** knob
        w = [i_crank * (a + bb * rpm * math.pi / 30) * rpm * math.pi / 30 for rpm in (40, 60, 90)]
        print(f"  {knob:>3}   {w[0]:5.0f} {w[1]:5.0f} {w[2]:5.0f}")
    if args.save:
        cal = {"i_crank": round(i_crank, 4), "flywheel_inertia": args.flywheel_inertia, "ratio": args.ratio,
               "a": a, "b0": round(b0, 5), "r": round(r, 5), "sessions": sessions,
               "note": "P = i_crank * (a + b0 * r**knob * w) * w, with w the crank speed in rad/s"}
        (ROOT / "calibration.json").write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n")
        print("\nCalibration written to calibration.json")


if __name__ == "__main__":
    main()
