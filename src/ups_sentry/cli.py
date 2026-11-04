"""The ups-sentry command."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ups_sentry.alerts import AlertFanout, Alerts, CommandAlerts, PushoverAlerts
from ups_sentry.config import ConfigError, NoEcoProfileError, load_settings, resolve_eco_auto_off
from ups_sentry.device import DeviceReadError, DeviceWriteError, SupportsEcoOff
from ups_sentry.drills import (
    COLUMNS,
    capacity_and_overhead_from_two_drains,
    measure_drain,
    overhead_from_one_drain,
    read_log,
    score,
)
from ups_sentry.drivers import create_device
from ups_sentry.heartbeat import Heartbeat
from ups_sentry.monitor import Monitor
from ups_sentry.status import read_status

log = logging.getLogger("ups_sentry")

# The monitor throttles reads itself; this only sets how promptly a due read starts.
_TICK_SECONDS = 10.0
_DRILL_POLL_SECONDS = 15.0


def _setup_logging(path: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if path:
        file = Path(path).expanduser()
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("bleak").setLevel(logging.WARNING)


def _build_alerts(settings: dict) -> Alerts:
    alerts = settings["alerts"]
    channels: list[Alerts] = []
    if alerts["pushover_api_token"] and alerts["pushover_user_key"]:
        channels.append(PushoverAlerts(alerts["pushover_api_token"], alerts["pushover_user_key"]))
    if alerts["command"]:
        channels.append(CommandAlerts(alerts["command"]))
    if not channels:
        log.warning("No alert channel configured: alerts only reach this log.")
    return AlertFanout(channels)


def _run(args: argparse.Namespace, settings: dict) -> int:
    _setup_logging(settings["log"]["path"])
    try:
        eco_auto_off = resolve_eco_auto_off(settings)
    except NoEcoProfileError as exc:
        log.error("%s", exc)
        raise
    alerts = _build_alerts(settings)
    monitor_settings, estimator = settings["monitor"], settings["estimator"]
    monitor = Monitor(
        alerts,
        device=create_device(settings),
        status_path=str(Path(settings["status"]["path"]).expanduser()),
        poll_interval_seconds=monitor_settings["poll_interval_seconds"],
        consecutive_reads_to_confirm_transition=monitor_settings["consecutive_reads_to_confirm_transition"],
        min_battery_pct_drop_to_confirm_outage=monitor_settings["min_battery_pct_drop_to_confirm_outage"],
        failures_to_page_unreachable=monitor_settings["failures_to_page_unreachable"],
        eco_auto_off=eco_auto_off,
        drain_window_minutes=estimator["drain_window_minutes"],
        capacity_wh=estimator["capacity_wh"],
        overhead_watts=estimator["overhead_watts"],
    )
    if not eco_auto_off:
        log.warning("monitor.eco_auto_off is off: if ECO is on, it can cut the output at low load. You'll only be alerted.")
    if not monitor.counts_energy:
        log.warning("No capacity_wh and overhead_watts for this model: minutes left comes from the battery percentage, "
                    "which reads long near empty. See `ups-sentry calibrate`.")
    heartbeat = None
    if settings["heartbeat"]["url"]:
        heartbeat = Heartbeat(settings["heartbeat"]["url"], alerts,
                              interval_seconds=settings["heartbeat"]["interval_seconds"]).start()
    alerts.alert("UPS monitor", f"Starting: status file {settings['status']['path']}")
    try:
        while True:
            monitor.maybe_poll_and_act(datetime.now(timezone.utc))
            if heartbeat is not None:
                heartbeat.beat()
            time.sleep(_TICK_SECONDS)
    except KeyboardInterrupt:
        alerts.alert("UPS monitor", "Stopped (Ctrl-C).")
        return 0
    except Exception as exc:
        alerts.alert("UPS monitor crashed", f"{exc} -- the status file goes stale until it's restarted.")
        raise


def _status(args: argparse.Namespace, settings: dict) -> int:
    status = read_status(settings["status"]["path"])
    if status is None:
        print(f"No status at {settings['status']['path']}; is the monitor running?", file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2))
    return 0


def _scan(args: argparse.Namespace, settings: dict) -> int:
    from bleak import BleakScanner

    found = asyncio.run(BleakScanner.discover(timeout=args.seconds))
    for device in sorted(found, key=lambda d: d.name or "~"):
        print(f"{device.address}  {device.name or '(no name)'}")
    return 0


def _probe(args: argparse.Namespace, settings: dict) -> int:
    state = create_device(settings).read_state()
    print(f"Battery {state.battery_pct}%; AC input {'present' if state.ac_input_present else 'absent'}; "
          f"output {state.output_watts} W; ECO on: {sorted(state.auto_off) if state.auto_off is not None else 'unknown'}")
    print(json.dumps(state.fields, indent=2))
    return 0


def _eco_off(args: argparse.Namespace, settings: dict) -> int:
    device = create_device(settings)
    if not isinstance(device, SupportsEcoOff):
        print("This driver can't switch ECO off.", file=sys.stderr)
        return 1
    outputs = frozenset(args.outputs or ("AC", "DC"))
    if not outputs <= {"AC", "DC"}:
        print(f"Unknown outputs {sorted(outputs - {'AC', 'DC'})}; only AC and DC.", file=sys.stderr)
        return 2
    if not args.yes and input(f"Write ECO off for {' and '.join(sorted(outputs))}? Type yes: ").strip().lower() != "yes":
        print("Nothing written.")
        return 0
    still_on = device.switch_eco_off(outputs)
    print(f"Done: the writes were echoed and the read-back shows ECO on for {sorted(still_on) or 'neither'}.")
    return 0


def _drill(args: argparse.Namespace, settings: dict) -> int:
    """Log a read every few seconds until Ctrl-C or the power goes; each row is on disk before the next read."""
    _setup_logging(None)
    device = create_device(settings)
    print(f"Logging to {args.log}. Unplug the station from the wall and leave it running to the cut: whatever the "
          "station powers loses power abruptly, so save your work.")
    with open(args.log, "a", newline="") as file:
        writer = csv.writer(file)
        if file.tell() == 0:
            writer.writerow(COLUMNS)
        try:
            while True:
                now = datetime.now(timezone.utc)
                try:
                    state = device.read_state()
                except DeviceReadError as exc:
                    writer.writerow([now.isoformat(), "", "", "", "", str(exc)])
                    print(f"{now:%H:%M:%S} read failed: {exc}")
                else:
                    writer.writerow([now.isoformat(), state.ac_input_present, state.battery_pct,
                                     "" if state.output_watts is None else state.output_watts,
                                     "" if state.vendor_minutes_left is None else state.vendor_minutes_left, ""])
                    print(f"{now:%H:%M:%S} battery {state.battery_pct}%, output {state.output_watts} W, "
                          f"AC input {'present' if state.ac_input_present else 'absent'}")
                file.flush()
                os.fsync(file.fileno())
                time.sleep(_DRILL_POLL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def _score(args: argparse.Namespace, settings: dict) -> int:
    estimator = settings["estimator"]
    result = score(read_log(args.log), capacity_wh=estimator["capacity_wh"], overhead_watts=estimator["overhead_watts"],
                   poll_seconds=settings["monitor"]["poll_interval_seconds"])
    print(f"Capacity {estimator['capacity_wh']} Wh, overhead {estimator['overhead_watts']} W")
    print(f"Median error, whole run:          {result.median_error:.1f} min")
    if result.vendor_median_error is not None:
        print(f"  the station's own estimate:     {result.vendor_median_error:.1f} min")
    print(f"Median error, last 60 min:        {result.median_error_last_60:.1f} min")
    print(f"Most too long, last 20 min:       {result.most_too_long_last_20:.1f} min")
    alarm = result.true_minutes_at_15_min_alarm
    print(f"True time left at the 15-min mark: {'never reached' if alarm is None else f'{alarm:.1f} min'}")
    if result.counted_wh_at_cut is not None:
        print(f"Counted energy at the cut:        {result.counted_wh_at_cut:+.1f} Wh (0 is exact)")
    return 0


def _calibrate(args: argparse.Namespace, settings: dict) -> int:
    drains = [measure_drain(read_log(path)) for path in args.logs]
    for path, drain in zip(args.logs, drains):
        print(f"{path}: {drain.hours:.2f} h from {drain.start_pct:.0f}% at {drain.average_output_watts:.1f} W output")
    if len(drains) == 2:
        capacity, overhead = capacity_and_overhead_from_two_drains(*drains)
    else:
        capacity = args.capacity_wh if args.capacity_wh is not None else settings["estimator"]["capacity_wh"]
        if capacity is None:
            print("One drain can't separate capacity from overhead: give --capacity-wh (the nameplate), or two logs.",
                  file=sys.stderr)
            return 1
        overhead = overhead_from_one_drain(drains[0], capacity)
    print("\nAssumes each log ran to the station's cut. Put these in your config:\n")
    print(f"[estimator]\ncapacity_wh = {capacity:.1f}\noverhead_watts = {overhead:.1f}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ups-sentry", description=__doc__)
    parser.add_argument("--config", help="config file (default ~/.config/ups-sentry/config.toml)")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="override one setting, e.g. --set estimator.overhead_watts=18; repeatable")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="watch the station and keep the status file current").set_defaults(func=_run)
    commands.add_parser("status", help="print the status file").set_defaults(func=_status)
    scan = commands.add_parser("scan", help="list nearby Bluetooth devices, to find the station's address")
    scan.add_argument("--seconds", type=float, default=10.0)
    scan.set_defaults(func=_scan)
    commands.add_parser("probe", help="read the station once and print everything it reports").set_defaults(func=_probe)
    eco = commands.add_parser("eco-off", help="switch ECO off now (stop the monitor first: they'd share the device)")
    # Not choices=: before Python 3.12, argparse rejects an empty list against them, so giving no outputs would fail.
    eco.add_argument("outputs", nargs="*", metavar="{AC,DC}", help="default: both")
    eco.add_argument("--yes", action="store_true", help="don't ask")
    eco.set_defaults(func=_eco_off)
    drill = commands.add_parser("drill", help="log readings during a drain to the cut")
    drill.add_argument("log")
    drill.set_defaults(func=_drill)
    score_command = commands.add_parser("score", help="score the estimate on a drill's log")
    score_command.add_argument("log")
    score_command.set_defaults(func=_score)
    calibrate = commands.add_parser("calibrate", help="work out capacity and overhead from one or two drills' logs")
    calibrate.add_argument("logs", nargs="+", metavar="log")
    calibrate.add_argument("--capacity-wh", type=float, help="with one log: the nameplate capacity")
    calibrate.set_defaults(func=_calibrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "calibrate" and len(args.logs) > 2:
        print("calibrate takes one or two logs.", file=sys.stderr)
        return 2
    try:
        settings = load_settings(args.config, args.set)
        return args.func(args, settings)
    except NoEcoProfileError:
        raise  # already logged; refusing to start must look like a failure to whatever launched it
    except (ConfigError, ValueError, DeviceReadError, DeviceWriteError) as exc:
        print(f"ups-sentry: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
