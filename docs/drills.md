# Drills

Unit tests can't tell you what a station really does at the end of its charge, so these run it for real.

**Before you start:** a drain to the cut **cuts power abruptly to whatever the station runs.** Save your work, stop anything you care
about, and stop the monitor, since only one program can talk to the station at a time. Run from a real Terminal window. A drain takes
hours: runtime is about capacity / (output + overhead).

```sh
ups-sentry drill drain.csv      # then unplug the station from the wall, and leave it running to the cut
```

Each row is on disk before the next read, so the last reading survives the cut. Afterwards, plug the station back in, start the computer,
and restart the monitor. Then `ups-sentry score drain.csv` scores the estimate, and `ups-sentry calibrate drain.csv` gives your unit's
numbers. Both refuse a log that stops well above empty, because they take its end as the cut.

## Unplug test, 2026-10-12

About 20 minutes off the wall. The outage was confirmed about 3 minutes after the unplug, once the battery was 2% down, and restored about a
minute after replugging. Battery 99% to 90% in 20 minutes: about 35 W leaving the battery against 12-39 W of output (12-16 W display off,
33-39 W display on), which gave the 15 W overhead the next drain tested.

## First deep drain, 2026-10-16

An Elite 10 powering a Mac, display off except for about 15 minutes (19-28 W against about 12 W). It ran about 275 minutes from 99%, and cut
its AC output right after a 1% reading. 499 readings, two Bluetooth hiccups, no gap over 3 minutes. The settings of every candidate were
fixed from the 10/12 test, before this drain. True time left = the time to the cut.

Percentage-based candidates, scored from when the outage was confirmed (97%, about 269 minutes before the cut):

| estimate | median error, all (min) | % too long, all | median error, last 60 min | % too long, last 60 min | % too long, last 20 min |
|---|---|---|---|---|---|
| the station's own | 16.9 | 76 | 4.1 | 52 | 81 |
| regression, 30 min | 11.9 | 67 | 6.3 | 74 | 100 |
| weighted, 5 min | 11.4 | 65 | 5.5 | 84 | 97 |
| weighted, 2.5 min | 13.7 | 64 | 6.7 | 94 | 94 |
| watts, raw | 7.0 | 75 | 2.4 | 77 | 97 |
| watts, smoothed | 6.1 | 71 | 1.8 | 70 | 100 |
| blend, weighted 5 min | 8.4 | 66 | 3.0 | 79 | 100 |
| blend, weighted 2.5 min | 9.0 | 68 | 3.8 | 90 | 97 |
| Kalman | 9.7 | 62 | 5.5 | 45 | 100 |

Near the end every one reads too long: the percentage falls fast (5% at 8 minutes left, 4% at 5, 1% at 2.6), and the station cuts at about
1%, not 0%. The station's battery management stops discharge early so the cells are never run flat.

Counting energy from watts instead: start from the first reading's energy, subtract (output + 15 W) x time, and never look at the
percentage again. The energy reached about zero at the cut, with the nameplate 128 Wh and an overhead fixed before the drain.

| estimate | median error, all (min) | median error, last 60 min | % too long, last 60 min | % too long, last 20 min | true time left when it first said 15 min |
|---|---|---|---|---|---|
| regression 30 min, percentage | 11.9 | 6.3 | 74 | 100 | 5.2 |
| Kalman, percentage + watts | 9.7 | 5.5 | 45 | 100 | 4.4 |
| smoothed watts, percentage energy | 6.1 | 1.8 | 70 | 100 | 8.1 |
| regression 30 min, counted | 4.5 | 0.4 | 14 | 0 | 15.1 |
| weighted 5 min, counted | 4.7 | 0.4 | 25 | 0 | 15.1 |
| weighted 2.5 min, counted | 4.7 | 0.6 | 33 | 0 | 15.1 |
| smoothed watts, counted | 5.2 | 1.0 | 35 | 0 | 17.0 |
| Kalman on the watts, counted | 4.9 | 1.0 | 37 | 0 | 15.1 |

Both tables are scored on every reading. The second, and the percentage rows it shares with the first, come from
`research/score_candidates.py`; the rest of the first come from the same log's columns
(`research/drain_2026-10-16_candidates.csv`).

The display test: the fast estimates fell within minutes when the display came on (right for "if this load continues"), while the slow
regression barely moved at first (about 30 minutes too long had the display stayed on, the dangerous direction), then read up to 18 minutes
short for about 20 minutes after it went off. So the estimate in use is the **lower of the 30-minute regression and 3-minute smoothed watts**,
taking the best of each.

That choice was made on this same drain, so these are in-sample numbers. Replayed at the live 2-minute polling (`ups-sentry score`):
median error 3.1 minutes over the whole run (the station's own 16.5), 0.2 in the last hour, never more than 0.2 too long in the last 20
minutes, and first said 15 minutes with 15.1 left. Counted energy at the cut: -0.7 Wh.

Caveats: one drain, one unit, one load. Counting needs a good starting energy (a whole-percent reading, about +-0.6 Wh), and drifts if the
overhead changes.

## Second deep drain, planned Fri 2026-11-13 or Sat 2026-11-14

A real test of the estimate, chosen on the drain above. Nothing is tuned: capacity 128 Wh, overhead 15 W, and the estimator as
`tests/test_regression.py` pins it. The one change is the load: **display on for the whole run,** about 25 W of output against about 12 W,
starting from 100%. The question is whether a fixed 15 W overhead holds at a higher load. If the inverter's draw grows with load, the estimate
reads long, the dangerous direction.

**Prediction:** the cut about 3.2 hours after unplugging (128 Wh at about 40 W), with counted energy near zero.

**Bars,** agreed 2026-11-03 before the run, judged at the live 2-minute polling by `python scripts/judge_second_drain.py second_drain.csv`:

| measure | pass | first drain (in-sample) |
|---|---|---|
| counted energy at the cut | within +-5 Wh | -0.7 Wh |
| median error, last 60 min | 3 min or less | 0.2 min |
| most too long, last 20 min | 2 min or less | 0.2 min |
| true time left when it first says 15 min | 10 min or more | 15.1 min |
| median error, whole run | beats the station's own on the same run | 3.1 min (16.5) |
