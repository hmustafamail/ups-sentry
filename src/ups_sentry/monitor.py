"""Watches a power station, publishes its state to the status file, and alerts on changes."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from ups_sentry.alerts import Alerts
from ups_sentry.device import DeviceReadError, DeviceState, DeviceWriteError, SupportsEcoOff, SupportsRead
from ups_sentry.estimator import RuntimeEstimator
from ups_sentry.status import SCHEMA_VERSION, write_status

log = logging.getLogger(__name__)


class Monitor:
    """Call `maybe_poll_and_act` often; it throttles itself to `poll_interval_seconds`.

    Every attempt rewrites the status file (`checked_at`), so readers can tell the monitor is alive; the station's own
    fields change only on a successful read (`last_ok_at`).

    A change of AC input needs `consecutive_reads_to_confirm_transition` polls in a row agreeing before it counts: a
    single misread of the input voltage would otherwise raise a false outage. An outage also needs the battery to have
    fallen by `min_battery_pct_drop_to_confirm_outage` across those polls, since a real outage drains it. A poll that
    fails outright neither counts toward nor resets this.
    """

    def __init__(
        self,
        alerts: Alerts,
        *,
        device: SupportsRead,
        status_path: str,
        poll_interval_seconds: float = 120.0,
        consecutive_reads_to_confirm_transition: int = 3,
        min_battery_pct_drop_to_confirm_outage: float = 2.0,
        failures_to_page_unreachable: int = 3,
        drain_window_minutes: float = 30.0,
        capacity_wh: float | None = None,
        overhead_watts: float | None = None,
        eco_auto_off: bool = False,
        status_writer: Callable[[str, dict], None] = write_status,
    ) -> None:
        """Set up polling, debouncing, the estimate and the status file.

        Parameters
        ----------
        alerts : Alerts
            Told about every power event.
        device : SupportsRead
            The driver.
        status_path : str
            The status file.
        poll_interval_seconds : float, optional
            Minimum gap between reads. Default 120.
        consecutive_reads_to_confirm_transition : int, optional
            Polls that must agree before AC input counts as changed. Default 3.
        min_battery_pct_drop_to_confirm_outage : float, optional
            Fall in battery % needed across those polls for an outage (not a recovery). Default 2. The window keeps
            extending until it has fallen this much; a rise restarts it.
        failures_to_page_unreachable : int, optional
            Failed reads in a row before "UPS unreachable". Default 3.
        drain_window_minutes, capacity_wh, overhead_watts
            See `RuntimeEstimator`.
        eco_auto_off : bool, optional
            Switch ECO off when it's seen on, if the driver can. False here because this class has no profile to go
            on; `ups-sentry run` decides it with `config.resolve_eco_auto_off`.
        status_writer : callable, optional
            (path, status) -> None. Defaults to an atomic file write.

        Raises
        ------
        ValueError
            If `consecutive_reads_to_confirm_transition` or `failures_to_page_unreachable` is less than 1, or
            `min_battery_pct_drop_to_confirm_outage` is negative.
        """
        if consecutive_reads_to_confirm_transition < 1:
            raise ValueError(f"consecutive_reads_to_confirm_transition must be >= 1, was "
                             f"{consecutive_reads_to_confirm_transition}")
        if min_battery_pct_drop_to_confirm_outage < 0:
            raise ValueError(f"min_battery_pct_drop_to_confirm_outage must be >= 0, was "
                             f"{min_battery_pct_drop_to_confirm_outage}")
        if failures_to_page_unreachable < 1:
            raise ValueError(f"failures_to_page_unreachable must be >= 1, was {failures_to_page_unreachable}")
        self._alerts = alerts
        self._device = device
        self._status_path = status_path
        self._status_writer = status_writer
        self._poll_interval = timedelta(seconds=poll_interval_seconds)
        self._consecutive_reads_to_confirm_transition = consecutive_reads_to_confirm_transition
        self._min_battery_pct_drop_to_confirm_outage = min_battery_pct_drop_to_confirm_outage
        self._failures_to_page_unreachable = failures_to_page_unreachable
        self._eco_auto_off = eco_auto_off
        self._estimator = RuntimeEstimator(capacity_wh=capacity_wh, overhead_watts=overhead_watts,
                                           drain_window_minutes=drain_window_minutes)

        self._last_polled_at: datetime | None = None
        self._ac_input_was_present = True
        self._transition_candidate: bool | None = None
        self._transition_candidate_count = 0
        self._transition_candidate_battery_pcts: list[float] = []

        self._last_ok_at: datetime | None = None
        self._battery_pct: float | None = None
        self._consecutive_failures = 0
        self._station_fields: dict[str, object] = {}
        self._auto_off_paged: frozenset[str] = frozenset()
        self._auto_off_unseen_logged = False

    @property
    def counts_energy(self) -> bool:
        """Whether the estimate counts energy (it has a capacity and an overhead)."""
        return self._estimator.counts_energy

    def maybe_poll_and_act(self, now: datetime) -> None:
        """Read the station if due, react to its state, and rewrite the status file.

        A no-op (no read, no write) if less than `poll_interval_seconds` has passed since the last poll.
        """
        if self._last_polled_at is not None and now - self._last_polled_at < self._poll_interval:
            return
        self._last_polled_at = now

        try:
            state = self._device.read_state()
        except DeviceReadError as exc:
            self._consecutive_failures += 1
            log.warning("Read failed: %s", exc)
            if self._consecutive_failures == self._failures_to_page_unreachable:
                self._alerts.alert("UPS unreachable", f"The UPS hasn't answered {self._consecutive_failures} reads "
                                   f"in a row ({exc}). Power status is stale until it does.")
            self._write_status(now, error=str(exc))
            return

        if self._consecutive_failures >= self._failures_to_page_unreachable:
            self._alerts.alert("UPS reachable again", "The UPS is answering again.")
        self._consecutive_failures = 0
        self._last_ok_at = now
        self._battery_pct = state.battery_pct
        self._station_fields = dict(state.fields)
        if state.fields:
            log.info("UPS reports %s", json.dumps(state.fields, default=str))
        if state.ac_input_present:
            self._estimator.reset()
        else:
            self._estimator.update(now, state.battery_pct, state.output_watts)
        self._react_to_outage_transition(state)
        self._check_auto_off(state)
        self._write_status(now)

    @property
    def power(self) -> str:
        """The confirmed power source: "wall", "battery", or "unknown" before the first successful read."""
        if self._last_ok_at is None:
            return "unknown"
        return "wall" if self._ac_input_was_present else "battery"

    def minutes_left(self) -> float | None:
        """On (confirmed) battery, minutes until empty; None otherwise or if unknown."""
        return self._estimator.minutes_left() if self.power == "battery" else None

    def _check_auto_off(self, state: DeviceState) -> None:
        """Switch off, and alert about, the station's own auto-off (ECO).

        AC ECO on is an emergency, on wall power or battery: it can switch the AC output off at low load, taking down
        whatever the UPS powers. Don't rely on a manual's "unavailable while charging". DC ECO does the same to the DC
        and USB ports, at high priority. Alerts when first seen on or when which outputs changes, and once at normal
        priority when both are off again; a failed switch-off is retried on every read.
        """
        on = state.auto_off
        if on is None:
            if state.fields and not self._auto_off_unseen_logged:
                self._auto_off_unseen_logged = True
                log.warning("This read didn't include the ECO settings, so they can't be checked on this read.")
            return
        if not on:
            if self._auto_off_paged:
                self._alerts.alert("UPS ECO off", "The UPS's ECO modes are off again.")
            self._auto_off_paged = frozenset()
            return
        names = " and ".join(f"{output} ECO" for output in sorted(on))
        one = len(on) == 1
        failure = self._try_eco_off(on)
        if failure is None:
            self._alerts.alert(
                "UPS ECO switched off",
                f"The UPS's {names} {'was' if one else 'were'} on; the monitor switched {'it' if one else 'them'} off "
                f"and read {'it' if one else 'them'} back off. Something switched {'it' if one else 'them'} on: the "
                "app, a button on the station, or a reset.")
            self._auto_off_paged = frozenset()
            return
        if on == self._auto_off_paged:
            return
        urgent = "AC" in on
        situation = ("AC ECO may switch the AC output off at low load, even on wall power, taking down whatever the "
                     "UPS powers." if urgent else "DC ECO switches the DC and USB ports off at low load.")
        self._alerts.alert(
            "UPS ECO is on",
            f"The UPS's {names} {'is' if one else 'are'} on. {situation} Turn {'it' if one else 'them'} off in the "
            f"vendor's app. {failure}",
            priority="emergency" if urgent else "high")
        self._auto_off_paged = on

    def _try_eco_off(self, on: frozenset[str]) -> str | None:
        """Switch ECO off for `on`; None if done, else why not (for the alert)."""
        if not self._eco_auto_off:
            return "Switching it off from here is disabled (monitor.eco_auto_off)."
        if not isinstance(self._device, SupportsEcoOff):
            return "The monitor can't switch it off on this UPS."
        try:
            still_on = self._device.switch_eco_off(on)
        except (DeviceWriteError, DeviceReadError) as exc:
            log.warning("Couldn't switch ECO off: %s", exc)
            return f"The monitor tried to switch it off and couldn't ({exc}); it tries again on every read."
        if still_on & on:
            return "The monitor tried to switch it off but it still reads on; it tries again on every read."
        return None

    def _write_status(self, now: datetime, *, error: str | None = None) -> None:
        status = {
            "schema_version": SCHEMA_VERSION,
            "checked_at": now.isoformat(),
            "last_ok_at": self._last_ok_at.isoformat() if self._last_ok_at else None,
            "power": self.power,
            "battery_pct": self._battery_pct,
            "minutes_left": self.minutes_left(),
            "error": error,
            "consecutive_failures": self._consecutive_failures,
            "station_fields": self._station_fields or None,
        }
        try:
            self._status_writer(self._status_path, status)
        except OSError as exc:
            log.error("Couldn't write %s: %s", self._status_path, exc)
            self._alerts.alert("Power status not written", f"Couldn't write {self._status_path}: {exc}")

    def _react_to_outage_transition(self, state: DeviceState) -> None:
        """Alert on AC input changing, once confirmed (see the class docstring)."""
        ac_input_present = state.ac_input_present
        if ac_input_present == self._ac_input_was_present:
            self._transition_candidate = None
            self._transition_candidate_count = 0
            self._transition_candidate_battery_pcts = []
            return

        battery_upticked = (
            not ac_input_present
            and ac_input_present == self._transition_candidate
            and self._transition_candidate_battery_pcts
            and state.battery_pct > self._transition_candidate_battery_pcts[-1]
        )
        if ac_input_present == self._transition_candidate and not battery_upticked:
            self._transition_candidate_count += 1
            self._transition_candidate_battery_pcts.append(state.battery_pct)
        else:
            self._transition_candidate = ac_input_present
            self._transition_candidate_count = 1
            self._transition_candidate_battery_pcts = [state.battery_pct]

        if self._transition_candidate_count < self._consecutive_reads_to_confirm_transition:
            return
        if not ac_input_present and not self._battery_confirms_discharge(self._transition_candidate_battery_pcts):
            return

        self._ac_input_was_present = ac_input_present
        self._transition_candidate = None
        self._transition_candidate_count = 0
        self._transition_candidate_battery_pcts = []

        if ac_input_present:
            self._alerts.alert("Power restored (UPS)",
                               f"Power station reports AC input restored (battery {state.battery_pct:.0f}%).")
        else:
            self._alerts.alert("Power outage (UPS)",
                               f"Power station reports AC input lost -- running on battery "
                               f"({state.battery_pct:.0f}%).", priority="high")

    def _battery_confirms_discharge(self, battery_pcts: list[float]) -> bool:
        """True if the drop from the first to the last confirming reading is at least the minimum."""
        return battery_pcts[0] - battery_pcts[-1] >= self._min_battery_pct_drop_to_confirm_outage
