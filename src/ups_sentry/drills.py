"""Drill logs: run a station on battery to its cut, then score the estimate and calibrate from the log.

A drill is the only way to get the truth about a station: the true time left at every reading is the time to the moment
it cut its output. It's also hard on whatever the station powers, which loses power abruptly at the end.

The log is a CSV with one row per read: time, ac_input_present, battery_pct, output_watts, vendor_minutes, error. Other
columns are ignored.
"""
from __future__ import annotations

import csv
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ups_sentry.device import DeviceReadError, DeviceState
from ups_sentry.monitor import Monitor

COLUMNS = ("time", "ac_input_present", "battery_pct", "output_watts", "vendor_minutes", "error")
# The log's last row is the last read before the cut; the cut itself came within one poll.
_CUT_AFTER_LAST_ROW = timedelta(seconds=15)
_MIN_LOAD_DIFFERENCE = 0.05  # the share by which two drains' average draws must differ to tell capacity from overhead
# Scoring and calibrating both take the log's end as the cut. Stations cut near empty (an Elite 10 at about 1%), so a log
# ending above this stopped early, and would otherwise give confident, wrong numbers.
_MOST_BATTERY_AT_THE_CUT_PCT = 10.0


@dataclass(frozen=True)
class Reading:
    time: datetime
    ac_input_present: bool
    battery_pct: float | None
    output_watts: float | None
    vendor_minutes: float | None
    error: str


def _number(text: str | None) -> float | None:
    return float(text) if text else None


def read_log(path: str) -> list[Reading]:
    """The drill's rows, in order."""
    with open(path, newline="") as file:
        return [Reading(datetime.fromisoformat(row["time"]), row["ac_input_present"].strip().lower() == "true",
                        _number(row["battery_pct"]), _number(row["output_watts"]), _number(row["vendor_minutes"]),
                        row["error"])
                for row in csv.DictReader(file)]


def _require_reached_the_cut(readings: list[Reading]) -> None:
    last_pct = next((r.battery_pct for r in reversed(readings) if r.battery_pct is not None), None)
    if last_pct is None or last_pct > _MOST_BATTERY_AT_THE_CUT_PCT:
        raise ValueError(f"This log ends at {last_pct}% battery; a drill must run to the cut, and these numbers take "
                         "the log's end as the cut.")


class _Silent:
    def alert(self, title: str, message: str, priority: str = "normal") -> None:
        pass


class _ReplayDevice:
    reading: Reading

    def read_state(self) -> DeviceState:
        if self.reading.error or self.reading.battery_pct is None:
            raise DeviceReadError(self.reading.error or "no reading")
        return DeviceState(battery_pct=self.reading.battery_pct, ac_input_present=self.reading.ac_input_present,
                           output_watts=self.reading.output_watts)


def replay(readings: list[Reading], *, poll_seconds: float, capacity_wh: float | None, overhead_watts: float | None,
           after_read: Callable[[Monitor], None] | None = None) -> list[tuple[str, float | None] | None]:
    """Run the real monitor over a log: for each reading (power, minutes left) if it polled that one, else None."""
    device = _ReplayDevice()
    monitor = Monitor(_Silent(), device=device, status_path="unused", poll_interval_seconds=poll_seconds,
                      capacity_wh=capacity_wh, overhead_watts=overhead_watts,
                      status_writer=lambda path, status: None)
    answers: list[tuple[str, float | None] | None] = []
    last_polled = None
    for reading in readings:
        device.reading = reading
        monitor.maybe_poll_and_act(reading.time)
        polled = monitor._last_polled_at != last_polled
        last_polled = monitor._last_polled_at
        answers.append((monitor.power, monitor.minutes_left()) if polled else None)
    if after_read is not None:
        after_read(monitor)
    return answers


@dataclass(frozen=True)
class Score:
    """How an estimate did on a drill, in minutes against the true time left."""

    median_error: float
    median_error_last_60: float
    most_too_long_last_20: float
    true_minutes_at_15_min_alarm: float | None
    vendor_median_error: float | None
    counted_wh_at_cut: float | None


def score(readings: list[Reading], *, capacity_wh: float | None, overhead_watts: float | None,
          poll_seconds: float = 120.0) -> Score:
    """Score the estimate at `poll_seconds` (default the live polling).

    Raises
    ------
    ValueError
        If the log has no estimate on battery to score, stopped before the cut, or has none near its end.
    """
    cut = readings[-1].time + _CUT_AFTER_LAST_ROW

    def true_left(reading: Reading) -> float:
        return (cut - reading.time).total_seconds() / 60

    answers = replay(readings, poll_seconds=poll_seconds, capacity_wh=capacity_wh, overhead_watts=overhead_watts)
    scored = [(reading, answer[1]) for reading, answer in zip(readings, answers)
              if answer is not None and answer[0] == "battery" and answer[1] is not None]
    if not scored:
        raise ValueError("No estimate on battery in this log; did the drill reach a confirmed outage?")
    errors = [(minutes - true_left(reading), true_left(reading)) for reading, minutes in scored]
    last_60 = [abs(error) for error, true in errors if true <= 60]
    last_20 = [error for error, true in errors if true <= 20]
    _require_reached_the_cut(readings)
    if not last_60 or not last_20:
        raise ValueError("This log has no estimate near its end, so there's no last hour to score.")
    vendor = [abs(reading.vendor_minutes - true_left(reading)) for reading, answer in zip(readings, answers)
              if answer is not None and answer[0] == "battery" and reading.vendor_minutes is not None]
    counted: list[float | None] = []
    replay(readings, poll_seconds=0, capacity_wh=capacity_wh, overhead_watts=overhead_watts,
           after_read=lambda monitor: counted.append(monitor._estimator.counted_wh))
    return Score(
        median_error=statistics.median(abs(error) for error, _ in errors),
        median_error_last_60=statistics.median(last_60),
        most_too_long_last_20=max(last_20),
        true_minutes_at_15_min_alarm=next((true_left(reading) for reading, minutes in scored if minutes <= 15), None),
        vendor_median_error=statistics.median(vendor) if vendor else None,
        counted_wh_at_cut=counted[0],
    )


@dataclass(frozen=True)
class Drain:
    """What one drill's log says about the energy used."""

    start_pct: float
    hours: float
    output_wh: float

    @property
    def average_output_watts(self) -> float:
        return self.output_wh / self.hours


def measure_drain(readings: list[Reading]) -> Drain:
    """The drain from the first on-battery reading with output watts to the last.

    Raises
    ------
    ValueError
        If the log has fewer than two such readings, or stopped before the cut.
    """
    _require_reached_the_cut(readings)
    drained = [r for r in readings if not r.ac_input_present and r.battery_pct is not None and r.output_watts is not None
               and not r.error]
    if len(drained) < 2:
        raise ValueError("Too few on-battery readings with output watts in this log")
    output_wh = sum(earlier.output_watts * (later.time - earlier.time).total_seconds() / 3600
                    for earlier, later in zip(drained, drained[1:]))
    return Drain(drained[0].battery_pct, (drained[-1].time - drained[0].time).total_seconds() / 3600, output_wh)


def overhead_from_one_drain(drain: Drain, capacity_wh: float) -> float:
    """The station's own draw, given its capacity: the energy that left the battery beyond what the output accounts for."""
    return (drain.start_pct / 100 * capacity_wh - drain.output_wh) / drain.hours


def capacity_and_overhead_from_two_drains(first: Drain, second: Drain) -> tuple[float, float]:
    """(capacity_wh, overhead_watts) from two drains to the cut, each solving start% x capacity = output + overhead x hours.

    Raises
    ------
    ValueError
        If the drains' loads are too close to tell capacity from overhead.
    """
    a, b = first.average_output_watts, second.average_output_watts
    if abs(a - b) < _MIN_LOAD_DIFFERENCE * max(a, b):
        raise ValueError(f"The two drains' loads ({a:.1f} W, {b:.1f} W) are too close to separate capacity from "
                         "overhead; run one at a clearly different load")
    determinant = first.start_pct / 100 * -second.hours + second.start_pct / 100 * first.hours
    capacity = (first.output_wh * -second.hours + second.output_wh * first.hours) / determinant
    overhead = (first.start_pct / 100 * second.output_wh - second.start_pct / 100 * first.output_wh) / determinant
    return capacity, overhead
