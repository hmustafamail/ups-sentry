"""Score the candidate minutes-left estimates against a drain's true time left (the time to the station's cut).

Percentage-based estimates (as the drill logged them) beside regression and Kalman applied to energy counted from
watts: the first reading's energy, then minus (output + 15 W overhead) x time, never looking at the percentage again
Also each counted estimate aimed at 1% (where the Bluetti cut) instead of 0%.

Usage:
    pip install numpy pandas
    python research/score_candidates.py [research/drain_2026-10-16_candidates.csv]
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CAP, OVER = 128.0, 15.0
d = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "drain_2026-10-16_candidates.csv"), parse_dates=["time"])
b = d[d.power == "battery"].reset_index(drop=True)
end = b.time.iloc[-1] + pd.Timedelta(seconds=15)
b["true"] = (end - b.time).dt.total_seconds() / 60
t = ((b.time - b.time.iloc[0]).dt.total_seconds() / 60).to_numpy()
w = b.ac_output_watts.to_numpy(float)
pct = b.battery_pct.to_numpy(float)

# Energy counted from watts: start at the first reading's energy, then subtract (output + overhead) x time.
E = np.empty(len(b))
E[0] = pct[0] / 100 * CAP
for i in range(1, len(b)):
    E[i] = E[i - 1] - (w[i - 1] + OVER) * (t[i] - t[i - 1]) / 60
b["energy_counted"] = E
b["energy_from_pct"] = pct / 100 * CAP

def regression(y, window=30.0, half=None, min_span=4.0):
    out = []
    for i in range(len(t)):
        m = (t[i] - t <= window) & (np.arange(len(t)) <= i)
        x, yy = t[m], y[m]
        if x[-1] - x[0] < min_span:
            out.append(np.nan)
            continue
        wt = np.ones_like(x) if half is None else 0.5 ** ((t[i] - x) / half)
        xm, ym = np.average(x, weights=wt), np.average(yy, weights=wt)
        slope = np.sum(wt * (x - xm) * (yy - ym)) / np.sum(wt * (x - xm) ** 2)
        out.append(y[i] / -slope if slope < 0 else np.nan)  # y in Wh, slope Wh/min
    return np.array(out)

def smoothed_watts(half=1.0):
    s = np.empty(len(t))
    s[0] = w[0]
    for i in range(1, len(t)):
        keep = 0.5 ** ((t[i] - t[i - 1]) / half)
        s[i] = keep * s[i - 1] + (1 - keep) * w[i]
    return s

def kalman_watts(q=0.5, r=4.0):
    """1-D Kalman on the draw itself (watts as the measurement, a slow random walk as the model)."""
    x, p, out = w[0], 4.0, []
    for i in range(len(t)):
        if i:
            p += q * (t[i] - t[i - 1])
            k = p / (p + r)
            x += k * (w[i] - x)
            p *= 1 - k
        out.append(x)
    return np.array(out)

sw, kw = smoothed_watts(), kalman_watts()
est = {
    # percentage-based, as the drill logged them
    "regression_30min (pct)": b.regression_30min.to_numpy(), "kalman (pct + watts)": b.kalman.to_numpy(),
    "watts_smoothed (pct energy)": b.watts_smoothed.to_numpy(),
    # energy counted from watts
    "regression_30min (counted)": regression(E), "weighted_5min (counted)": regression(E, half=5.0),
    "weighted_2_5min (counted)": regression(E, half=2.5),
    "smoothed watts (counted)": E / (sw + OVER) * 60,
    "kalman on watts (counted)": E / (kw + OVER) * 60,
}
floor_wh = 1.0 / 100 * CAP  # the Bluetti cut at about 1%
for name in list(est):
    if "counted" in name:
        e = est[name]
        est[name.replace("(counted)", "(counted, to 1%)")] = e * np.clip(E - floor_wh, 0, None) / np.where(E > 0, E, np.nan)

print(f"counted energy at the cut: {E[-1]:.1f} Wh ({E[-1]/CAP*100:.1f}%); battery read {pct[-1]:.0f}%; "
      f"counted vs pct-energy gap: max {np.max(np.abs(E - pct/100*CAP)):.1f} Wh")
print(f"\n{'estimate':34}{'med|err| all':>13}{'% long all':>11}{'med|err| 60':>12}{'% long 60':>10}{'% long 20':>10}{'first <=15 at':>14}")
for name, e in est.items():
    err = e - b.true.to_numpy()
    def stats(mask):
        x = err[mask & ~np.isnan(err)]  # noqa: B023 - called within this pass of the loop
        return np.median(np.abs(x)), (x > 0).mean() * 100
    a, al = stats(np.ones(len(t), bool))
    s60, l60 = stats(b.true.to_numpy() <= 60)
    _, l20 = stats(b.true.to_numpy() <= 20)
    first = b.true[(e <= 15)].iloc[0] if (e <= 15).any() else math.nan
    print(f"{name:34}{a:13.1f}{al:11.0f}{s60:12.1f}{l60:10.0f}{l20:10.0f}{first:14.1f}")
