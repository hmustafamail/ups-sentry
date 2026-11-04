"""Tests for src/ups_sentry/drills.py. pytest tests/test_drills.py -v"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ups_sentry.drills import (
    Drain,
    Reading,
    capacity_and_overhead_from_two_drains,
    measure_drain,
    overhead_from_one_drain,
    read_log,
    score,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "drain_2026-10-16.csv")
START = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def reading(minutes, pct=50.0, watts=10.0, *, ac=False, error=""):
    return Reading(START + timedelta(minutes=minutes), ac, pct, watts, None, error)


class TestMeasureDrain:
    def test_the_real_drain(self):
        drain = measure_drain(read_log(FIXTURE))
        assert drain.start_pct == 99
        assert 4.5 < drain.hours < 4.7
        assert 12 < drain.average_output_watts < 14

    def test_leaves_out_wall_power_and_failed_reads(self):
        log = [reading(0, 100, 99, ac=True), reading(1, 90, 10), reading(2, None, None, error="Timeout"),
               reading(61, 5, 10)]
        drain = measure_drain(log)
        assert (drain.start_pct, drain.hours, drain.output_wh) == (90, 1.0, 10.0)

    def test_too_few_readings(self):
        with pytest.raises(ValueError, match="Too few"):
            measure_drain([reading(0, 5)])

    def test_a_log_that_stops_short_of_the_cut_is_refused(self):
        """Naive version: a drill stopped at 50% would calibrate to a confidently wrong overhead."""
        with pytest.raises(ValueError, match="run to the cut"):
            measure_drain([reading(0, 90), reading(60, 50)])


class TestCalibration:
    def test_one_drain_gives_the_overhead(self):
        assert overhead_from_one_drain(Drain(100.0, 4.0, 60.0), 100.0) == pytest.approx(10.0)

    def test_one_real_drain_comes_back_to_about_15_watts(self):
        """The energy counted at 15 W ended within a Wh of zero, so calibrating that drain should return about 15 W."""
        assert overhead_from_one_drain(measure_drain(read_log(FIXTURE)), 128.0) == pytest.approx(15.0, abs=0.5)

    def test_two_drains_recover_capacity_and_overhead(self):
        capacity, overhead = 120.0, 14.0
        light = Drain(100.0, capacity / (12 + overhead), 12 * capacity / (12 + overhead))
        heavy = Drain(90.0, 0.9 * capacity / (40 + overhead), 40 * 0.9 * capacity / (40 + overhead))
        assert capacity_and_overhead_from_two_drains(light, heavy) == pytest.approx((capacity, overhead))

    def test_two_drains_at_the_same_load_are_refused(self):
        drain = Drain(100.0, 4.0, 48.0)
        with pytest.raises(ValueError, match="too close"):
            capacity_and_overhead_from_two_drains(drain, drain)


class TestScore:
    def test_a_log_that_stops_short_of_the_cut_is_refused(self):
        """Naive version: its end would be taken as the cut, and the estimate scored against a false time left."""
        log = [reading(n, 90 - n, 12.0) for n in range(0, 20, 2)]
        with pytest.raises(ValueError, match="run to the cut"):
            score(log, capacity_wh=1000.0, overhead_watts=15.0)

    def test_a_log_without_an_outage_is_refused(self):
        with pytest.raises(ValueError, match="confirmed outage"):
            score([reading(n, 100, 12.0, ac=True) for n in range(0, 20, 2)], capacity_wh=128.0, overhead_watts=15.0)
