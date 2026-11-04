"""Tests for src/ups_sentry/estimator.py. pytest tests/test_estimator.py -v

Most of its behavior is tested through the monitor (tests/test_monitor.py) and on a real drain (tests/test_regression.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ups_sentry.estimator import RuntimeEstimator

START = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


class TestRuntimeEstimator:
    @pytest.mark.parametrize("kwargs, match", [({"capacity_wh": 0}, "capacity_wh"), ({"capacity_wh": -1}, "capacity_wh"),
                                               ({"overhead_watts": -1}, "overhead_watts")])
    def test_rejects_impossible_numbers(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            RuntimeEstimator(**kwargs)

    def test_zero_overhead_is_allowed(self):
        assert RuntimeEstimator(capacity_wh=100, overhead_watts=0).counts_energy

    def test_nothing_before_a_reading(self):
        assert RuntimeEstimator(capacity_wh=100, overhead_watts=10).minutes_left() is None

    def test_counts_from_the_first_reading(self):
        estimator = RuntimeEstimator(capacity_wh=100, overhead_watts=10)
        estimator.update(START, 50, 10)  # 50 Wh at 20 W
        assert estimator.minutes_left() == 150.0
        estimator.update(START + timedelta(minutes=30), 50, 10)  # 10 Wh used, whatever the percentage says
        assert estimator.counted_wh == pytest.approx(40)

    def test_reset_forgets_the_outage(self):
        estimator = RuntimeEstimator(capacity_wh=100, overhead_watts=10)
        estimator.update(START, 50, 10)
        estimator.reset()
        assert estimator.minutes_left() is None and estimator.counted_wh is None

    def test_never_negative_past_empty(self):
        estimator = RuntimeEstimator(capacity_wh=10, overhead_watts=10)
        estimator.update(START, 10, 10)
        estimator.update(START + timedelta(hours=5), 1, 10)
        assert estimator.counted_wh < 0
        assert estimator.minutes_left() == 0.0
