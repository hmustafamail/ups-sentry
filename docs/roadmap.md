# Roadmap

## Next

- [ ] **Second deep drain**, Fri 2026-11-13 or Sat 2026-11-14: a real test of the estimate at a higher load
  ([drills.md](drills.md)). If it fails, calibrate overhead against load, which needs a third drain to confirm.
- [ ] **Run `ups-sentry run` for real.** `probe` has worked on the Elite 10 (2026-11-04), but the monitor loop, ECO write and
  alerts have only run on real hardware in the code this was extracted from.
- [ ] After the second drain: calibrate capacity and overhead from the two drains, and update the README's table and the
  Elite 10 profile.

## Later

- [ ] Test on Linux, where many UPS monitors would run. Scanning and connection handling may differ.
- [ ] Publish to PyPI (the name was free on 2026-11-03).
- [ ] Packaged background services: launchd and systemd examples. On macOS, Bluetooth permission is the hard part.
- [ ] A longer drain to test an energy-and-overhead Kalman filter with the percentage as a slow correction
  (`research/runtime_estimators.py`). Counted energy drifts if the overhead changes, for instance under a heavier load or on
  a cold battery, so a long outage may want the percentage as a correction.
- [ ] Aged batteries: nameplate capacity will read long on an old unit, so consider an estimate of capacity from each outage.
- [ ] Explain the first failed ECO write on 2026-10-12 for certain ([history.md](history.md)). It hasn't recurred.
- [ ] Drop the private-method dependency if `bluetti-bt-lib` ever supports encrypted writes. Its issue #2 says they're planned,
  and asks for no comments there.
