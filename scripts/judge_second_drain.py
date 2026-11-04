"""Judge the second deep drain against the bars agreed before it ran (docs/drills.md).

Usage:
    python scripts/judge_second_drain.py second_drain.csv
"""
from __future__ import annotations

import argparse
import math
import sys

from ups_sentry.drills import read_log, score

# Frozen: the estimate's settings for this test, and its bars.
CAPACITY_WH, OVERHEAD_WATTS = 128.0, 15.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log")
    result = score(read_log(parser.parse_args().log), capacity_wh=CAPACITY_WH, overhead_watts=OVERHEAD_WATTS)
    energy = result.counted_wh_at_cut if result.counted_wh_at_cut is not None else math.nan
    alarm = result.true_minutes_at_15_min_alarm if result.true_minutes_at_15_min_alarm is not None else math.nan
    vendor = result.vendor_median_error if result.vendor_median_error is not None else math.inf
    bars = [
        (f"counted energy at the cut {energy:+.1f} Wh", "within +-5 Wh", abs(energy) <= 5),
        (f"median error, last 60 min {result.median_error_last_60:.1f} min", "3 min or less",
         result.median_error_last_60 <= 3),
        (f"most too long, last 20 min {result.most_too_long_last_20:.1f} min", "2 min or less",
         result.most_too_long_last_20 <= 2),
        (f"true time left at the 15-min alarm {alarm:.1f} min", "10 min or more", alarm >= 10),
        (f"median error, whole run {result.median_error:.1f} min (the station's own {vendor:.1f})",
         "beats the station's own", result.median_error < vendor),
    ]
    for measure, bar, passed in bars:
        print(f"{'PASS' if passed else 'FAIL'}  {measure}  (bar: {bar})")
    passed = all(passed for _, _, passed in bars)
    print("All bars passed." if passed else "At least one bar failed.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
