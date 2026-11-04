"""Tests for research/runtime_estimators.py. pytest tests/test_research_estimators.py -v"""
from __future__ import annotations

import pytest
from runtime_estimators import (
    KalmanEstimate,
    RegressionEstimate,
    SideBySide,
    UpsReading,
    WattsEstimate,
)


def drain(pct_per_minute, minutes, *, start_pct=100.0, watts=20.0, every=0.5, start=0.0):
    """Readings on a straight line (no rounding), every `every` minutes."""
    count = int(minutes / every) + 1
    return [UpsReading(start + i * every, start_pct - pct_per_minute * i * every, watts) for i in range(count)]


class TestRegressionEstimate:
    def test_nothing_until_the_readings_span_four_minutes(self):
        estimate = RegressionEstimate()
        results = [estimate.update(r) for r in drain(1.0, 4.0)]
        assert all(result is None for result in results[:-1])
        assert results[-1] == pytest.approx(96.0)  # 96% left at 1%/min

    @pytest.mark.parametrize("half_life", [None, 5.0, 2.5])
    def test_a_straight_line_gives_the_same_answer_however_weighted(self, half_life):
        estimate = RegressionEstimate(half_life_minutes=half_life)
        for reading in drain(0.5, 10.0):
            result = estimate.update(reading)
        assert result == pytest.approx(95.0 / 0.5)

    def test_weighting_reacts_sooner_when_the_drain_slows(self):
        """The display goes off: 1%/min for 20 min, then 0.25%/min. The true answer moves from 80 to 300 min."""
        before = drain(1.0, 20.0)
        after = drain(0.25, 6.0, start_pct=80.0, start=20.5)
        equal, weighted = RegressionEstimate(), RegressionEstimate(half_life_minutes=2.5)
        for reading in before + after:
            equal_result, weighted_result = equal.update(reading), weighted.update(reading)
        assert weighted_result > equal_result

    def test_no_estimate_while_the_battery_is_not_falling(self):
        estimate = RegressionEstimate()
        for reading in drain(0.0, 6.0):
            result = estimate.update(reading)
        assert result is None


class TestWattsEstimate:
    def test_energy_over_output_plus_overhead(self):
        # 50% of 128 Wh = 64 Wh at 49 W + 15 W overhead = 64 W: one hour.
        assert WattsEstimate().update(UpsReading(0.0, 50.0, 49.0)) == pytest.approx(60.0)

    def test_smoothing_moves_halfway_in_one_half_life(self):
        estimate = WattsEstimate(smoothing_half_life_minutes=1.0)
        estimate.update(UpsReading(0.0, 50.0, 10.0))
        estimate.update(UpsReading(1.0, 50.0, 30.0))
        assert estimate.watts == pytest.approx(20.0)


class TestKalmanEstimate:
    def test_learns_an_overhead_the_watts_leave_out(self):
        """The battery falls as if 20 W more than the output were drawn; the filter starts at 15 W."""
        estimate = KalmanEstimate()
        for i in range(120):  # an hour of half-minute readings
            minutes = i * 0.5
            energy = 128.0 - (20.0 + 20.0) * minutes / 60
            estimate.update(UpsReading(minutes, round(energy / 128.0 * 100), 20.0))
        assert estimate.overhead_watts == pytest.approx(20.0, abs=2.0)

    def test_reacts_at_once_to_a_change_in_draw(self):
        estimate = KalmanEstimate()
        for reading in drain(0.25, 10.0, watts=15.0):
            low = estimate.update(reading)
        high = estimate.update(UpsReading(10.5, 97.4, 60.0))
        assert high < low * 0.8


class TestSideBySide:
    def test_every_column_every_reading_blends_once_both_halves_exist(self):
        side_by_side = SideBySide()
        first = side_by_side.update(UpsReading(0.0, 99.0, 30.0))
        assert list(first) == list(SideBySide.COLUMNS)
        assert first["regression_30min"] is None and first["blend_weighted_5min"] is None
        assert first["watts_raw"] is not None and first["kalman"] is not None
        for reading in drain(0.5, 5.0, start_pct=98.5, watts=30.0, start=0.5):
            last = side_by_side.update(reading)
        assert last["blend_weighted_5min"] == pytest.approx((last["weighted_5min"] + last["watts_smoothed"]) / 2)
