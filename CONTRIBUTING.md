# Contributing

PRs that add support for more devices are welcome. If the checks pass and the change only adds your device, it should
go in without much back and forth.

```sh
pip install -e ".[dev]"
pytest
ruff check .
```

## Add a model of a brand that's already supported

Most of the time reads already work if the driver's library knows your model. Run `ups-sentry probe` to see.

To get the good estimate, add your model to [profiles.toml](src/ups_sentry/profiles.toml):

```toml
[bluetti.AC180]
capacity_wh = 1152.0
overhead_watts = 20.0
source = "Nameplate; overhead from one drain to the cut at 60 W output."
```

Get `overhead_watts` from `ups-sentry calibrate` (see the README). A measured number from one unit is fine; say so in
`source`. Without a measurement, leave out both numbers rather than guessing: a low overhead makes the estimate read long,
which is the dangerous direction.

If the library knows your model's ECO switches, `eco-off` works without any profile entry. Adding `eco_registers` is
optional, and tells the driver to stop if the library's register map ever changes; the tests check it against the library.

## Add a brand

Add a module in [drivers/](src/ups_sentry/drivers/) with a class implementing `read_state()` from
[device.py](src/ups_sentry/device.py), and add it to `create_device`. Raise `DeviceReadError` for any failed read, so the
monitor can handle them all the same way. `switch_eco_off` is optional.

Keep vendor libraries imported inside the driver module, so nobody pays for a brand they don't use. If the library has a
private API you rely on, pin its exact version.

## Rules that aren't negotiable

These protect the machines behind someone's UPS:

- A write may only ever switch auto-off (ECO) off. No other setting, no other value.
- Every write must be confirmed: the device's reply checked, and the setting read back.
- `monitor.eco_auto_off` stays off by default.

The estimator is the part most easily broken without anyone noticing. `tests/test_regression.py` holds it to its recorded
answers on a real drain, so a change to the estimate must be deliberate, and must say why those numbers moved.
