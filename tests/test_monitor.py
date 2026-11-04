"""Tests for src/ups_sentry/monitor.py. pytest tests/test_monitor.py -v"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from ups_sentry.device import DeviceReadError, DeviceState, DeviceWriteError
from ups_sentry.monitor import Monitor


class FakeAlerts:
    """Keeps every alert as (message, category, details), so a test can look at the title and priority."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def alert(self, title, message, priority="normal"):
        self.entries.append((message, "alert", {"page": True, "title": title, "urgent": priority == "emergency",
                                                "high": priority == "high"}))


class FakePowerStation:
    """Returns states from a pre-seeded queue, or raises, one per `read_state()` call."""

    def __init__(self, states: list[DeviceState] | None = None) -> None:
        self._states = list(states or [])
        self.read_calls = 0

    def read_state(self) -> DeviceState:
        self.read_calls += 1
        if not self._states:
            raise DeviceReadError("no more fake states queued")
        return self._states.pop(0)


class FakeFileStore:
    """Keeps every status written, in memory."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def write_json(self, path, payload) -> None:
        self.writes.append((str(path), payload))

    @property
    def last(self) -> dict:
        return self.writes[-1][1]


@pytest.fixture
def fake_log() -> FakeAlerts:
    return FakeAlerts()


def make_monitor(
    *,
    fake_log,
    states=None,
    poll_interval_seconds=100.0,
    consecutive_reads_to_confirm_transition=1,
    min_battery_pct_drop_to_confirm_outage=0.0,
    failures_to_page_unreachable=3,
) -> tuple[Monitor, FakePowerStation, FakeFileStore]:
    power_station = FakePowerStation(states)
    file_store = FakeFileStore()
    monitor = Monitor(
        fake_log,
        device=power_station,
        status_path="power_status.json",
        capacity_wh=128.0,
        overhead_watts=15.0,
        poll_interval_seconds=poll_interval_seconds,
        consecutive_reads_to_confirm_transition=consecutive_reads_to_confirm_transition,
        min_battery_pct_drop_to_confirm_outage=min_battery_pct_drop_to_confirm_outage,
        failures_to_page_unreachable=failures_to_page_unreachable,
        status_writer=file_store.write_json,
    )
    return monitor, power_station, file_store


START = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


class TestInit:
    @pytest.mark.parametrize("consecutive_reads", [0, -1, -5])
    def test_raises_for_non_positive_consecutive_reads(self, fake_log, consecutive_reads):
        with pytest.raises(ValueError, match="consecutive_reads_to_confirm_transition"):
            make_monitor(fake_log=fake_log, consecutive_reads_to_confirm_transition=consecutive_reads)

    def test_accepts_one(self, fake_log):
        make_monitor(fake_log=fake_log, consecutive_reads_to_confirm_transition=1)  # must not raise

    @pytest.mark.parametrize("min_drop", [-0.1, -1, -100])
    def test_raises_for_negative_min_battery_pct_drop(self, fake_log, min_drop):
        with pytest.raises(ValueError, match="min_battery_pct_drop_to_confirm_outage"):
            make_monitor(fake_log=fake_log, min_battery_pct_drop_to_confirm_outage=min_drop)

    def test_accepts_zero_min_battery_pct_drop(self, fake_log):
        make_monitor(fake_log=fake_log, min_battery_pct_drop_to_confirm_outage=0.0)  # must not raise


class TestPollThrottling:
    def test_first_call_always_polls(self, fake_log):
        monitor, power_station, _ = make_monitor(
            fake_log=fake_log,
            states=[DeviceState(battery_pct=100, ac_input_present=True)],
        )
        monitor.maybe_poll_and_act(START)
        assert power_station.read_calls == 1

    def test_second_call_within_interval_does_not_poll(self, fake_log):
        monitor, power_station, _ = make_monitor(
            fake_log=fake_log,
            states=[DeviceState(battery_pct=100, ac_input_present=True)],
            poll_interval_seconds=100.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=50))
        assert power_station.read_calls == 1

    def test_call_after_interval_elapses_polls_again(self, fake_log):
        monitor, power_station, _ = make_monitor(
            fake_log=fake_log,
            states=[
                DeviceState(battery_pct=100, ac_input_present=True),
                DeviceState(battery_pct=99, ac_input_present=True),
            ],
            poll_interval_seconds=100.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=150))
        assert power_station.read_calls == 2


class TestReadFailure:
    def test_read_error_is_logged_quietly_and_does_not_raise(self, fake_log, caplog):
        monitor, _, _ = make_monitor(fake_log=fake_log, states=[])
        with caplog.at_level(logging.WARNING):
            monitor.maybe_poll_and_act(START)  # must not raise
        assert "Read failed" in caplog.text
        assert fake_log.entries == []


class TestOutageTransitionAlerts:
    def test_no_alert_while_ac_input_stays_present(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            states=[DeviceState(battery_pct=100, ac_input_present=True)],
        )
        monitor.maybe_poll_and_act(START)
        assert fake_log.entries == []

    def test_ac_input_lost_pages_probable_outage(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            states=[DeviceState(battery_pct=90, ac_input_present=False)],
        )
        monitor.maybe_poll_and_act(START)

        assert len(fake_log.entries) == 1
        message, category, kwargs = fake_log.entries[0]
        assert category == "alert"
        assert kwargs.get("page") is True
        assert kwargs.get("high") is True
        assert not kwargs.get("urgent")
        assert kwargs.get("title") == "Power outage (UPS)"

    def test_ac_input_restored_pages_recovery(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=91, ac_input_present=True),
            ],
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))

        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)", "Power restored (UPS)"]
        # losing power is high priority (past Do Not Disturb, not repeated); getting it back is normal
        assert [bool(kwargs.get("high")) for _, _, kwargs in fake_log.entries] == [True, False]
        assert not any(kwargs.get("urgent") for _, _, kwargs in fake_log.entries)

    def test_transition_alert_fires_only_once_while_outage_continues(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=85, ac_input_present=False),
            ],
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))

        outage_alerts = [e for e in fake_log.entries if e[2].get("title") == "Power outage (UPS)"]
        assert len(outage_alerts) == 1


class TestTransitionDebounce:
    """`consecutive_reads_to_confirm_transition` (default 2 in production,
    overridden to 1 by `make_monitor` for the rest of this file) guards
    against a single glitchy BLE read flipping outage state. A naive
    version that flips on the first disagreeing read would pass
    `test_second_disagreeing_read_confirms_and_pages` too, so these
    specifically check the *lone-glitch* case stays silent."""

    def test_single_disagreeing_read_does_not_yet_page(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            states=[DeviceState(battery_pct=90, ac_input_present=False)],
            consecutive_reads_to_confirm_transition=2,
        )
        monitor.maybe_poll_and_act(START)

        assert fake_log.entries == []
        assert monitor.power != "battery"

    def test_second_disagreeing_read_confirms_and_pages(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=89, ac_input_present=False),
            ],
            consecutive_reads_to_confirm_transition=2,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))

        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)"]
        assert monitor.power == "battery"

    def test_intervening_agreeing_read_resets_the_candidate_count(self, fake_log):
        """A lone glitch followed by a normal reading, followed later by
        another lone glitch, must never accumulate across the gap --
        each glitch needs its own run of confirming reads."""
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),  # glitch
                DeviceState(battery_pct=90, ac_input_present=True),  # back to normal
                DeviceState(battery_pct=90, ac_input_present=False),  # glitch again
            ],
            consecutive_reads_to_confirm_transition=2,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))
        monitor.maybe_poll_and_act(START + timedelta(seconds=2))

        assert fake_log.entries == []
        assert monitor.power != "battery"

    def test_read_failure_between_confirming_reads_does_not_reset_candidate(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                # queue empties here -> next poll's read_state() raises
            ],
            consecutive_reads_to_confirm_transition=2,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))  # read fails
        assert monitor.power != "battery"


class TestBatteryDrainConfirmsOutage:
    """An outage candidate also needs its confirming polls' battery% to
    show a real drain of >= min_battery_pct_drop_to_confirm_outage --
    guards against a real AC-input misread that coincides with an
    otherwise-flat battery (e.g. an idle load on a full charge), which
    a poll-count-only debounce would still confirm as a real outage."""

    def test_poll_count_met_but_battery_flat_does_not_page(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=90, ac_input_present=False),
            ],
            consecutive_reads_to_confirm_transition=2,
            min_battery_pct_drop_to_confirm_outage=1.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))

        assert fake_log.entries == []
        assert monitor.power != "battery"

    def test_poll_count_and_sufficient_drop_both_met_pages(self, fake_log):
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=88, ac_input_present=False),
            ],
            consecutive_reads_to_confirm_transition=2,
            min_battery_pct_drop_to_confirm_outage=1.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))

        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)"]
        assert monitor.power == "battery"

    def test_window_extends_past_poll_count_until_drop_is_sufficient(self, fake_log):
        """Naive version: a fix that only checked the drop at exactly the
        `consecutive_reads_to_confirm_transition`th poll (rather than
        letting the window keep growing) would give up here instead of
        confirming on the 3rd poll."""
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=89.5, ac_input_present=False),
                DeviceState(battery_pct=88, ac_input_present=False),
            ],
            consecutive_reads_to_confirm_transition=2,
            min_battery_pct_drop_to_confirm_outage=2.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))
        assert fake_log.entries == []

        monitor.maybe_poll_and_act(START + timedelta(seconds=2))
        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)"]

    def test_battery_uptick_restarts_the_window(self, fake_log):
        """A battery% increase mid-window (mains charging it) contradicts
        "running on battery" outright, so the readings before it are
        discarded rather than just diluting the average drop."""
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),  # candidate starts
                DeviceState(battery_pct=91, ac_input_present=False),  # uptick -> restarts
                DeviceState(battery_pct=89, ac_input_present=False),  # 2nd read of new window
            ],
            consecutive_reads_to_confirm_transition=2,
            min_battery_pct_drop_to_confirm_outage=1.0,
        )
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=1))
        assert fake_log.entries == [], "uptick should have restarted the window, not confirmed it"

        monitor.maybe_poll_and_act(START + timedelta(seconds=2))
        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)"]

    def test_recovery_does_not_require_a_battery_drop(self, fake_log):
        """Only a candidate *outage* needs battery evidence -- a candidate
        recovery pages purely on poll-count agreement, regardless of what
        the battery% is doing."""
        monitor, _, _ = make_monitor(
            fake_log=fake_log,
            poll_interval_seconds=0,
            states=[
                DeviceState(battery_pct=90, ac_input_present=False),
                DeviceState(battery_pct=85, ac_input_present=False),  # outage confirmed
                DeviceState(battery_pct=85, ac_input_present=True),
                DeviceState(battery_pct=84, ac_input_present=True),  # still confirms restore
            ],
            consecutive_reads_to_confirm_transition=2,
            min_battery_pct_drop_to_confirm_outage=1.0,
        )
        for i in range(4):
            monitor.maybe_poll_and_act(START + timedelta(seconds=i))

        titles = [kwargs.get("title") for _, _, kwargs in fake_log.entries]
        assert titles == ["Power outage (UPS)", "Power restored (UPS)"]


def battery(pct: float) -> DeviceState:
    return DeviceState(battery_pct=pct, ac_input_present=False)


def wall(pct: float = 100) -> DeviceState:
    return DeviceState(battery_pct=pct, ac_input_present=True)


class TestStatusFile:
    def test_unknown_until_the_first_successful_read(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[])
        monitor.maybe_poll_and_act(START)
        status = store.last
        assert status["power"] == "unknown"
        assert status["checked_at"] == START.isoformat()
        assert status["last_ok_at"] is None
        assert status["battery_pct"] is None
        assert status["error"] == "no more fake states queued"
        assert status["consecutive_failures"] == 1

    def test_wall_power_after_a_good_read(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[wall(97)])
        monitor.maybe_poll_and_act(START)
        assert store.last == {"checked_at": START.isoformat(), "last_ok_at": START.isoformat(), "power": "wall",
                              "battery_pct": 97, "minutes_left": None, "error": None, "consecutive_failures": 0,
                              "station_fields": None, "schema_version": 1}

    def test_failed_read_keeps_the_last_good_values_and_updates_checked_at(self, fake_log):
        monitor, power_station, store = make_monitor(fake_log=fake_log, states=[wall(97)])
        monitor.maybe_poll_and_act(START)
        later = START + timedelta(seconds=200)
        monitor.maybe_poll_and_act(later)  # queue empty: read fails
        status = store.last
        assert status["checked_at"] == later.isoformat()
        assert status["last_ok_at"] == START.isoformat()
        assert (status["power"], status["battery_pct"]) == ("wall", 97)
        assert status["error"]

    def test_power_is_debounced_like_the_pages(self, fake_log):
        """A single noisy battery read doesn't flip `power` before the outage is confirmed."""
        monitor, _, store = make_monitor(fake_log=fake_log, states=[wall(), battery(99)],
                                         consecutive_reads_to_confirm_transition=2)
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=200))
        assert store.last["power"] == "wall"

    def test_throttled_call_writes_nothing(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[wall()])
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=10))
        assert len(store.writes) == 1


class TestMinutesLeft:
    def _outage(self, fake_log, pcts, minutes_apart=2.0):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[battery(p) for p in pcts],
                                         poll_interval_seconds=1.0)
        for n in range(len(pcts)):
            monitor.maybe_poll_and_act(START + timedelta(minutes=minutes_apart * n))
        return monitor, store

    def test_none_on_wall_power(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[wall(90)])
        monitor.maybe_poll_and_act(START)
        assert store.last["minutes_left"] is None

    def test_none_with_too_short_a_span(self, fake_log):
        _, store = self._outage(fake_log, [90, 89], minutes_apart=2.0)  # 2 minutes < 4
        assert store.last["power"] == "battery"
        assert store.last["minutes_left"] is None

    def test_estimates_from_the_observed_drain(self, fake_log):
        # 1% every 2 minutes from 90%: at 87% after 6 minutes, 0.5%/min -> 174 minutes left.
        _, store = self._outage(fake_log, [90, 89, 88, 87])
        assert store.last["minutes_left"] == pytest.approx(174.0)

    def test_none_when_not_draining(self, fake_log):
        _, store = self._outage(fake_log, [88, 88, 88, 88], minutes_apart=3.0)
        assert store.last["power"] == "battery"
        assert store.last["minutes_left"] is None

    def test_old_readings_leave_the_window(self, fake_log):
        """Only the last 30 minutes count: a fast early drain doesn't dominate once it has passed."""
        pcts = [100, 90] + [80 - 0.5 * n for n in range(20)]  # fast first, then 0.5% per 2 minutes
        monitor, store = self._outage(fake_log, pcts)
        assert store.last["minutes_left"] == pytest.approx(store.last["battery_pct"] / 0.25, rel=0.01)

    def test_a_wall_reading_resets_the_estimate(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[battery(90), battery(88), wall(88),
                                                                    battery(87)],
                                         poll_interval_seconds=1.0)
        for n in range(4):
            monitor.maybe_poll_and_act(START + timedelta(minutes=3 * n))
        assert store.last["minutes_left"] is None  # one reading since the wall read


def battery_at(pct: float, watts: float | None) -> DeviceState:
    return DeviceState(battery_pct=pct, ac_input_present=False, output_watts=watts)


class TestCountedMinutesLeft:
    """Energy counted from output watts (128 Wh, 15 W of the station's own draw, by default)."""

    def _outage(self, fake_log, readings, minutes_apart=2.0):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[battery_at(p, w) for p, w in readings],
                                         poll_interval_seconds=1.0)
        for n in range(len(readings)):
            monitor.maybe_poll_and_act(START + timedelta(minutes=minutes_apart * n))
        return monitor, store

    def test_first_reading_uses_the_smoothed_watts_alone(self, fake_log):
        # 50% of 128 Wh = 64 Wh at 12 + 15 = 27 W -> 142.2 minutes.
        _, store = self._outage(fake_log, [(50, 12)])
        assert store.last["minutes_left"] == pytest.approx(64 / 27 * 60, abs=0.1)

    def test_steady_load_counts_the_energy_down(self, fake_log):
        """The battery % is held flat, so a percentage-based estimate would give None: only counting moves it."""
        _, store = self._outage(fake_log, [(50, 12)] * 4)  # 6 minutes at 27 W uses 2.7 Wh
        assert store.last["minutes_left"] == pytest.approx((64 - 2.7) / 27 * 60, abs=0.1)

    def test_a_load_rise_shows_within_minutes(self, fake_log):
        """The regression still averages the quiet stretch; the smoothed watts sees the rise, and the lower wins."""
        quiet = [(50, 12)] * 16  # 30 minutes at 12 W
        monitor, store = self._outage(fake_log, quiet + [(50, 40)] * 3)
        energy = monitor._estimator.counted_wh
        assert store.last["minutes_left"] < energy / 27 * 60 - 30
        assert store.last["minutes_left"] == pytest.approx(energy / ((monitor._estimator._smoothed_watts + 15) / 60), abs=0.1)

    def test_a_load_drop_stays_cautious_until_the_regression_catches_up(self, fake_log):
        busy = [(50, 40)] * 16
        monitor, store = self._outage(fake_log, busy + [(50, 12)] * 3)
        smoothed_only = monitor._estimator.counted_wh / ((monitor._estimator._smoothed_watts + 15) / 60)
        assert store.last["minutes_left"] < smoothed_only

    def test_a_single_spike_barely_moves_it(self, fake_log):
        monitor, store = self._outage(fake_log, [(50, 12)] * 16)
        before = store.last["minutes_left"]
        monitor._device._states.extend([battery_at(50, 30)])
        monitor.maybe_poll_and_act(START + timedelta(minutes=32))
        assert before - store.last["minutes_left"] < 0.25 * before

    def test_falls_back_to_the_battery_percentage_without_output_watts(self, fake_log):
        # 1% every 2 minutes from 90%: at 87% after 6 minutes, 0.5%/min -> 174 minutes left.
        _, store = self._outage(fake_log, [(90, None), (89, None), (88, None), (87, None)])
        assert store.last["minutes_left"] == pytest.approx(174.0)

    def test_a_wall_reading_restarts_the_count(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[battery_at(50, 12), battery_at(49, 12),
                                                                    wall(60), battery_at(60, 12)],
                                         poll_interval_seconds=1.0)
        for n in range(4):
            monitor.maybe_poll_and_act(START + timedelta(minutes=3 * n))
        assert monitor._estimator.counted_wh == pytest.approx(0.6 * 128)

    def test_capacity_and_overhead_are_settings(self, fake_log):
        monitor = Monitor(fake_log, device=FakePowerStation([battery_at(50, 10)]), status_path="s",
                          poll_interval_seconds=1.0, consecutive_reads_to_confirm_transition=1,
                          min_battery_pct_drop_to_confirm_outage=0.0, capacity_wh=200, overhead_watts=10,
                          status_writer=FakeFileStore().write_json)
        monitor.maybe_poll_and_act(START)
        assert monitor.minutes_left() == pytest.approx(100 / 20 * 60)

    @pytest.mark.parametrize("capacity, overhead", [(None, None), (128.0, None), (None, 15.0)])
    def test_without_both_it_uses_the_battery_percentage(self, fake_log, capacity, overhead):
        """Naive version: a missing overhead taken as 0 W would count energy and read long, the dangerous direction."""
        states = [battery_at(p, 12) for p in (90, 89, 88, 87)]
        monitor = Monitor(fake_log, device=FakePowerStation(states), status_path="s", poll_interval_seconds=1.0,
                          consecutive_reads_to_confirm_transition=1, min_battery_pct_drop_to_confirm_outage=0.0,
                          capacity_wh=capacity, overhead_watts=overhead, status_writer=FakeFileStore().write_json)
        for n in range(4):
            monitor.maybe_poll_and_act(START + timedelta(minutes=2 * n))
        assert monitor.counts_energy is False
        assert monitor.minutes_left() == pytest.approx(174.0)


class TestUnreachable:
    def test_pages_once_after_n_failures_in_a_row(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[], failures_to_page_unreachable=3,
                                         poll_interval_seconds=1.0)
        for n in range(5):
            monitor.maybe_poll_and_act(START + timedelta(minutes=n))
        pages = [kw.get("title") for _, _, kw in fake_log.entries if kw.get("page")]
        assert pages == ["UPS unreachable"]
        assert store.last["consecutive_failures"] == 5

    def test_unreachable_page_is_not_urgent(self, fake_log):
        monitor, _, _ = make_monitor(fake_log=fake_log, states=[], failures_to_page_unreachable=1)
        monitor.maybe_poll_and_act(START)
        (_, _, kwargs), = [e for e in fake_log.entries if e[2].get("page")]
        assert not kwargs.get("urgent")

    def test_pages_when_reachable_again(self, fake_log):
        power_station_states = []
        monitor, power_station, _ = make_monitor(fake_log=fake_log, states=power_station_states,
                                                 failures_to_page_unreachable=2, poll_interval_seconds=1.0)
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(minutes=1))
        power_station._states.append(wall())
        monitor.maybe_poll_and_act(START + timedelta(minutes=2))
        pages = [kw.get("title") for _, _, kw in fake_log.entries if kw.get("page")]
        assert pages == ["UPS unreachable", "UPS reachable again"]

    def test_no_recovery_page_when_never_paged(self, fake_log):
        monitor, power_station, _ = make_monitor(fake_log=fake_log, states=[], failures_to_page_unreachable=3,
                                                 poll_interval_seconds=1.0)
        monitor.maybe_poll_and_act(START)
        power_station._states.append(wall())
        monitor.maybe_poll_and_act(START + timedelta(minutes=1))
        assert not [e for e in fake_log.entries if e[2].get("page")]

    @pytest.mark.parametrize("failures", [0, -1])
    def test_raises_for_non_positive_failures(self, fake_log, failures):
        with pytest.raises(ValueError, match="failures_to_page_unreachable"):
            make_monitor(fake_log=fake_log, failures_to_page_unreachable=failures)


def with_eco(state: DeviceState, on, **fields) -> DeviceState:
    """`on`: the outputs with ECO on, e.g. {"AC"}; True for {"AC"}, False for none, None for not reported."""
    auto_off = None if on is None else frozenset({"AC"} if on is True else on or ())
    return DeviceState(battery_pct=state.battery_pct, ac_input_present=state.ac_input_present,
                             auto_off=auto_off, fields={"ctrl_eco_ac": on, "time_remaining": 1.5, **fields})


def pages(fake_log) -> list[tuple[str, bool, bool]]:
    return [(kw["title"], kw.get("urgent", False), kw.get("high", False)) for _, _, kw in fake_log.entries
            if kw.get("page")]


class TestStationFields:
    def test_every_field_is_logged_and_kept_in_the_status_file(self, fake_log, caplog):
        state = with_eco(wall(97), False, ac_output_power=42)
        monitor, _, store = make_monitor(fake_log=fake_log, states=[state])
        with caplog.at_level(logging.INFO):
            monitor.maybe_poll_and_act(START)
        assert "ac_output_power" in caplog.text and "time_remaining" in caplog.text
        assert store.last["station_fields"] == {"ctrl_eco_ac": False, "time_remaining": 1.5, "ac_output_power": 42}

    def test_a_failed_read_keeps_the_last_fields(self, fake_log):
        monitor, _, store = make_monitor(fake_log=fake_log, states=[with_eco(wall(97), False)])
        monitor.maybe_poll_and_act(START)
        monitor.maybe_poll_and_act(START + timedelta(seconds=200))  # queue empty: read fails
        assert store.last["station_fields"]["time_remaining"] == 1.5


def run(fake_log, states):
    monitor, _, _ = make_monitor(fake_log=fake_log, states=states)
    for i in range(len(states)):
        monitor.maybe_poll_and_act(START + timedelta(seconds=200 * i))
    return pages(fake_log)


class TestAutoOff:
    """AC ECO may cut the output even on wall power, so it's an emergency; DC ECO is high priority."""

    def test_ac_on_while_on_wall_power_is_an_emergency_paged_once(self, fake_log):
        """Naive version (the manual's): ECO is inactive while charging, so a high page until an outage. Wall power
        or not, AC ECO on can take the output down."""
        assert run(fake_log, [with_eco(wall(), True), with_eco(wall(), True)]) == [("UPS ECO is on", True, False)]

    def test_dc_on_pages_high_and_never_as_an_emergency(self, fake_log):
        states = [with_eco(wall(), {"DC"}), with_eco(battery(90), {"DC"}), with_eco(battery(89), {"DC"})]
        eco = [p for p in run(fake_log, states) if p[0] == "UPS ECO is on"]
        assert eco == [("UPS ECO is on", False, True)]
        assert any("DC ECO is on" in m for m, _, _ in fake_log.entries)

    def test_ac_switched_on_after_dc_is_an_emergency(self, fake_log):
        states = [with_eco(wall(), {"DC"}), with_eco(wall(), {"AC", "DC"})]
        assert run(fake_log, states) == [("UPS ECO is on", False, True), ("UPS ECO is on", True, False)]
        assert "AC ECO and DC ECO are on" in [m for m, _, kw in fake_log.entries if kw.get("page")][-1]

    def test_an_outage_with_ac_on_doesnt_page_it_again(self, fake_log):
        states = [with_eco(wall(), True), with_eco(battery(90), True), with_eco(battery(89), True)]
        eco = [p for p in run(fake_log, states) if p[0] == "UPS ECO is on"]
        assert eco == [("UPS ECO is on", True, False)]

    def test_off_again_pages_normally_and_rearms(self, fake_log):
        states = [with_eco(wall(), True), with_eco(wall(), False), with_eco(wall(), True)]
        assert run(fake_log, states) == [("UPS ECO is on", True, False), ("UPS ECO off", False, False),
                                         ("UPS ECO is on", True, False)]

    def test_off_pages_nothing(self, fake_log):
        assert run(fake_log, [with_eco(wall(), False)]) == []

    def test_a_read_without_the_settings_changes_nothing_and_is_noted_once(self, fake_log, caplog):
        states = [with_eco(wall(), True), with_eco(wall(), None), with_eco(wall(), None)]
        with caplog.at_level(logging.WARNING):
            assert [p[0] for p in run(fake_log, states)] == ["UPS ECO is on"]
        assert caplog.text.count("didn't include the ECO settings") == 1


class FakeEcoStation(FakePowerStation):
    """Also switches ECO off: each call takes the next outcome (a set still on, or an exception to raise)."""

    def __init__(self, states, outcomes) -> None:
        super().__init__(states)
        self._outcomes = list(outcomes)
        self.switched: list[frozenset[str]] = []

    def switch_eco_off(self, outputs: frozenset[str]) -> frozenset[str]:
        self.switched.append(frozenset(outputs))
        outcome = self._outcomes.pop(0) if self._outcomes else frozenset()
        if isinstance(outcome, Exception):
            raise outcome
        return frozenset(outcome)


def run_eco(fake_log, states, outcomes=(), eco_auto_off=True):
    station = FakeEcoStation(states, outcomes)
    monitor = Monitor(fake_log, device=station, status_path="power_status.json",
                      poll_interval_seconds=100.0, consecutive_reads_to_confirm_transition=1,
                      min_battery_pct_drop_to_confirm_outage=0.0, eco_auto_off=eco_auto_off,
                      status_writer=FakeFileStore().write_json)
    for i in range(len(states)):
        monitor.maybe_poll_and_act(START + timedelta(seconds=200 * i))
    return station, pages(fake_log)


class TestEcoSwitchedOff:
    """The monitor switches ECO off itself when configured to."""

    def test_ac_on_is_switched_off_and_paged_normally(self, fake_log):
        station, paged = run_eco(fake_log, [with_eco(wall(), {"AC", "DC"}), with_eco(wall(), False)])
        assert station.switched == [frozenset({"AC", "DC"})]
        assert paged == [("UPS ECO switched off", False, False)]

    def test_dc_only_switched_off_pages_normally(self, fake_log):
        _, paged = run_eco(fake_log, [with_eco(wall(), {"DC"})])
        assert paged == [("UPS ECO switched off", False, False)]

    def test_a_failure_pages_as_before_with_the_reason_and_retries_every_read(self, fake_log):
        states = [with_eco(wall(), True), with_eco(wall(), True), with_eco(wall(), False)]
        station, paged = run_eco(fake_log, states, outcomes=[DeviceWriteError("No reply"), frozenset()])
        assert len(station.switched) == 2
        assert paged == [("UPS ECO is on", True, False), ("UPS ECO switched off", False, False)]
        first = [m for m, _, kw in fake_log.entries if kw.get("title") == "UPS ECO is on"][0]
        assert "couldn't (No reply)" in first

    def test_still_reading_on_counts_as_a_failure(self, fake_log):
        _, paged = run_eco(fake_log, [with_eco(wall(), True)], outcomes=[frozenset({"AC"})])
        assert paged == [("UPS ECO is on", True, False)]

    def test_failures_page_once_per_set_of_outputs(self, fake_log):
        error = DeviceReadError("Timeout")
        station, paged = run_eco(fake_log, [with_eco(wall(), True)] * 3, outcomes=[error] * 3)
        assert len(station.switched) == 3 and paged == [("UPS ECO is on", True, False)]

    def test_off_by_default(self, fake_log):
        station = FakeEcoStation([with_eco(wall(), True)], ())
        monitor = Monitor(fake_log, device=station, status_path="s", status_writer=FakeFileStore().write_json)
        monitor.maybe_poll_and_act(START)
        assert station.switched == []

    def test_disabled_only_pages(self, fake_log):
        station, paged = run_eco(fake_log, [with_eco(wall(), True)], eco_auto_off=False)
        assert station.switched == [] and paged == [("UPS ECO is on", True, False)]
        assert "disabled" in [m for m, _, kw in fake_log.entries if kw.get("page")][0]
