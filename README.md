# ups-sentry

Run a portable power station as a real UPS: know when the power goes out, know how long you really have, and stop the
station from switching itself off.

Not affiliated with or endorsed by BLUETTI. It writes to your station's settings if you ask it to, and is offered with
no warranty; see [LICENSE](LICENSE).

## Why

Power stations make decent UPSes, with three problems this fixes:

- **ECO mode can cut your output.** It switches the AC or DC output off at low load, which takes down a small server
  sitting behind it. On encrypted BLUETTI models the usual library refuses writes, so you can only fix it from the app.
  ups-sentry switches it off and keeps it off.
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
as a best case. A second drain at a higher load is planned as a real test ([docs/drills.md](docs/drills.md)). Reproduce the numbers above with
`ups-sentry --set device.driver=bluetti --set device.model=EL10 score tests/fixtures/drain_2026-10-16.csv`.

## Supported

| | Status |
|---|---|
| BLUETTI Elite 10 (EL10) on macOS | this code ran for weeks inside its original application: reads, ECO off and the estimate |
| Other models [bluetti-bt-lib](https://github.com/Patrick762/bluetti-bt-lib) knows | reads should work; untested. `run` needs a choice about ECO (below) |
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

With ECO off the inverter stays live all the time, so it draws from the battery continuously. That's the overhead the
estimate counts, and why the runtime is shorter than capacity / load suggests.

## Troubleshooting

**"Could not recognize BLUETTI device" on every read.** Check whether the vendor's app is open or connected on a phone. These
stations allow one Bluetooth connection at a time, so a phone holding it starves the monitor, even though nothing is wrong
with the pairing or the address. Close the app and wait for the next read.

**The monitor reports an outage that didn't happen.** A single misread of the input voltage can't do this, since an outage
needs 3 reads in a row and a 2% fall in the battery. Raise `monitor.consecutive_reads_to_confirm_transition` or
`monitor.min_battery_pct_drop_to_confirm_outage` if you still see one.

## More

- [docs/drills.md](docs/drills.md): how to test a station to its cut, with results.
- [docs/history.md](docs/history.md): what was learned building this, and why it works the way it does.
- [docs/roadmap.md](docs/roadmap.md): what's next.
- `scripts/`: diagnostics (the key exchange, a computer's own draw beside the station's). `research/`: the candidate
  estimates compared on the first drain.

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

ups-sentry writes to a station only to switch ECO off. `ups-sentry run` does that **by default** on a model with a profile,
because the alternative is the station cutting power to your computer for drawing too little. To be alerted instead, set
`monitor.eco_auto_off = false`.

A model without a profile refuses to start, with an error, until you choose: `true` uses the driver library's register
map (untested on that model), and `false` only alerts you when ECO is on. Better still, add the model's profile.

On every write it writes only the ECO switches and only the value 0; stops if the library's register map disagrees with the
model's profile; requires the station to echo the write exactly; and reads the switch back on the same connection.

`bluetti-bt-lib` is pinned to an exact version because the encrypted write subclasses its private methods.

## License

MIT
