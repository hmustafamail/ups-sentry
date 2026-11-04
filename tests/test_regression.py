"""Holds the estimate to its recorded answers on a real drain. pytest tests/test_regression.py -v

fixtures/drain_2026-10-16.csv is a real drain of a BLUETTI Elite 10 to its cut, with what the original monitor answered
at every reading and at 2-minute polling. A change that moves these numbers must be deliberate.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ups_sentry.drills import read_log, replay, score

FIXTURE = str(Path(__file__).parent / "fixtures" / "drain_2026-10-16.csv")
CAPACITY_WH, OVERHEAD_WATTS = 128.0, 15.0


def _expected(suffix: str) -> list[tuple[str, float | None] | None]:
    with open(FIXTURE, newline="") as file:
        return [(row[f"expected_power_{suffix}"],
                 float(row[f"expected_minutes_{suffix}"]) if row[f"expected_minutes_{suffix}"] else None)
                if row[f"expected_power_{suffix}"] else None
                for row in csv.DictReader(file)]


class TestRecordedAnswers:
    @pytest.mark.parametrize("suffix, poll_seconds", [("every", 1.0), ("live", 120.0)])
    def test_same_power_and_minutes_as_the_original_monitor_at_every_reading(self, suffix, poll_seconds):
        answers = replay(read_log(FIXTURE), poll_seconds=poll_seconds, capacity_wh=CAPACITY_WH,
                         overhead_watts=OVERHEAD_WATTS)
        assert answers == _expected(suffix)

    def test_the_fixture_exercises_what_it_claims(self):
        """Naive version: a fixture of blanks would pass the comparison above."""
        expected = _expected("live")
        minutes = [answer[1] for answer in expected if answer is not None and answer[1] is not None]
        assert len(minutes) > 100 and max(minutes) > 200 and min(minutes) < 5
        assert {answer[0] for answer in expected if answer is not None} >= {"wall", "battery"}


class TestScore:
    def test_the_numbers_the_readme_quotes(self):
        result = score(read_log(FIXTURE), capacity_wh=CAPACITY_WH, overhead_watts=OVERHEAD_WATTS)
        assert result.median_error == pytest.approx(3.1, abs=0.05)
        assert result.median_error_last_60 == pytest.approx(0.2, abs=0.05)
        assert result.most_too_long_last_20 == pytest.approx(0.2, abs=0.05)
        assert result.true_minutes_at_15_min_alarm == pytest.approx(15.1, abs=0.05)
        assert result.vendor_median_error == pytest.approx(16.5, abs=0.05)
        assert result.counted_wh_at_cut == pytest.approx(-0.7, abs=0.05)
