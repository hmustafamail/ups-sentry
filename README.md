# ups-sentry

Run a portable power station as a real UPS: know when the power goes out, know how long you really have, and stop the
station from switching itself off.

Not affiliated with or endorsed by BLUETTI. It writes to your station's settings if you ask it to, and is offered with
no warranty; see [LICENSE](LICENSE).

## Why

Power stations make decent UPSes, with three problems this fixes:

- **ECO mode can cut your output.** It switches the AC or DC output off at low load, which takes down a small server
  sitting behind it. On encrypted BLUETTI models the usual library refuses writes, so you can only fix it from the app.
  ups-sentry can switch it off and keep it off.
- **The station's own time-left estimate is not good enough to act on.** On a BLUETTI Elite 10 it leaves out the
  inverter's own draw of about 15 W and jumps with every spike in load (196-301 minutes within minutes).
- **Nothing tells your other software.** ups-sentry keeps a small JSON status file that any program can read.

## How well the estimate works

ups-sentry counts energy rather than trusting the battery percentage: the first reading's percentage of the capacity,
minus (output + the station's own draw) x time. On one 275-minute drain of an Elite 10 to its cut, at about 12 W output
and scored at 2-minute polling:

| | ups-sentry | the station's own |
|---|---|---|
| median error, whole run | 3.1 min | 16.5 min |
| median error, last hour | 0.2 min | |
| most too long, last 20 min | 0.2 min | |
| true time left when it first said 15 min | 15.1 min | |

**This is one drain of one unit at one load, and the method was chosen on that same drain,** so take the small numbers
as a best case. A second drain at a higher load is planned as a real test. Reproduce the numbers above with
`ups-sentry --set device.driver=bluetti --set device.model=EL10 score tests/fixtures/drain_2026-10-16.csv`.

## Supported

| | Status |
|---|---|
| BLUETTI Elite 10 (EL10) on macOS | this code ran for weeks inside its original application: reads, ECO off and the estimate |
| Other models [bluetti-bt-lib](https://github.com/Patrick762/bluetti-bt-lib) knows | reads should work; untested |
| ECO off on other models | where the library knows the switch; untested. Every write is checked (below) |
| Linux | should work (Bluetooth goes through [bleak](https://github.com/hbldh/bleak)); untested |

A model with no capacity and overhead in [profiles.toml](src/ups_sentry/profiles.toml) falls back to an estimate from the
battery percentage, which reads long near empty, until you calibrate it. Other brands need a driver:
see [CONTRIBUTING.md](CONTRIBUTING.md).

## Getting started

```sh
pip install git+https://github.com/hmustafamail/ups-sentry   # Python 3.11 or later
ups-sentry scan                                              # find your station's address
mkdir -p ~/.config/ups-sentry
cp config.example.toml ~/.config/ups-sentry/config.toml      # set device.model and device.address
ups-sentry probe                                             # read it once
ups-sentry run                                               # keep watching
```

On macOS, only a process started from a real logged-in Terminal window can use Bluetooth. A background or SSH shell hangs.

Only one program can talk to the station at a time, so close the vendor's app and stop the monitor before `probe`,
`eco-off` or `drill`.

## Settings

Settings come in layers: built-in defaults, then the model's profile, then your config file, then `--set` on the command
line. [config.example.toml](config.example.toml) lists them all. A misspelled setting is an error rather than being
ignored.

```sh
ups-sentry --set estimator.overhead_watts=18 run
```

## Calibrating

Your unit's numbers can differ from the profile's: batteries age, and the inverter's draw may change with load. To measure
them, drain the station to its cut, which **cuts power abruptly to whatever it's running**:

```sh
ups-sentry drill first.csv      # unplug it and leave it running to the cut
ups-sentry score first.csv      # how did the estimate do?
ups-sentry calibrate first.csv --capacity-wh 128     # one drain: overhead, given the capacity
ups-sentry calibrate first.csv second.csv            # two drains at different loads: both
```

## The status file

Written atomically, so a reader never sees half a file. Fields are documented in
[status.py](src/ups_sentry/status.py); `schema_version` changes only if a field changes meaning or goes away.

```json
{
  "schema_version": 1,
  "checked_at": "2026-11-13T22:00:00+00:00",
  "last_ok_at": "2026-11-13T22:00:00+00:00",
  "power": "battery",
  "battery_pct": 62.0,
  "minutes_left": 118.4,
  "error": null,
  "consecutive_failures": 0,
  "station_fields": {"ac_output_power": 25, "ctrl_eco_ac": false}
}
```

If `checked_at` is old, the monitor isn't running: treat the power state as unknown.

## Writes

ups-sentry writes to a station only to switch ECO off, and only if you run `eco-off` or set `monitor.eco_auto_off`.
On every write it writes only the ECO switches and only the value 0; stops if the library's register map disagrees with the
model's profile; requires the station to echo the write exactly; and reads the switch back on the same connection.

`bluetti-bt-lib` is pinned to an exact version because the encrypted write subclasses its private methods.

## License

MIT
