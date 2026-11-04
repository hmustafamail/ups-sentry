"""Candidate estimates of minutes left, fed the same readings side by side, as scored on the 2026-10-16 drain.

Research only: the estimate in use is `ups_sentry.estimator`. Settings were fixed from an earlier unplug test, before that
drain, so the drain tested them rather than fitted them. docs/drills.md has the results; `score_candidates.py` reproduces
them. An energy-and-overhead Kalman filter with the percentage as a slow correction is the candidate for a longer test.
"""
from __future__ import annotations

import math
from typing import ClassVar, NamedTuple


class UpsReading(NamedTuple):
    """One successful UPS read on battery."""

    minutes: float  # since any fixed origin, increasing
    battery_pct: float
    output_watts: float


class RegressionEstimate:
    """Least-squares line through the battery % readings (the live monitor's method), optionally weighted.

    `half_life_minutes` None: equal weights inside `window_minutes` (the monitor's 30-min fit). Otherwise each
    reading's weight halves every `half_life_minutes` back (exponentially weighted; the window still bounds it).
    Minutes left = the latest battery % / the fitted rate of decline, once the readings span `min_span_minutes`.
    """

    def __init__(self, *, window_minutes: float = 30.0, half_life_minutes: float | None = None,
                 min_span_minutes: float = 4.0) -> None:
        self._window = window_minutes
        self._half_life = half_life_minutes
        self._min_span = min_span_minutes
        self._readings: list[UpsReading] = []

    def update(self, reading: UpsReading) -> float | None:
        """Add a reading; the minutes left, or None while too few readings span too little time or it isn't falling."""
        self._readings.append(reading)
        self._readings = [r for r in self._readings if reading.minutes - r.minutes <= self._window]
        if reading.minutes - self._readings[0].minutes < self._min_span:
            return None
        weights = [1.0 if self._half_life is None else 0.5 ** ((reading.minutes - r.minutes) / self._half_life)
                   for r in self._readings]
        total = sum(weights)
        x_mean = sum(w * r.minutes for w, r in zip(weights, self._readings)) / total
        y_mean = sum(w * r.battery_pct for w, r in zip(weights, self._readings)) / total
        spread = sum(w * (r.minutes - x_mean) ** 2 for w, r in zip(weights, self._readings))
        slope = sum(w * (r.minutes - x_mean) * (r.battery_pct - y_mean)
                    for w, r in zip(weights, self._readings)) / spread
        if slope >= 0:
            return None
        return reading.battery_pct / -slope


class WattsEstimate:
    """Energy left / (output watts + the inverter's own draw), with the watts optionally smoothed.

    Energy left assumes the nameplate capacity is all usable down to 0%; the deep drain says whether it is.
    `smoothing_half_life_minutes` None: the latest watts as read. Otherwise an exponential average whose weight on
    the past halves every that many minutes (time-based, so it doesn't depend on the polling rate).
    """

    CAPACITY_WH: ClassVar[float] = 128.0  # Bluetti Elite 10
    # The 10/12 unplug drill: about 31.8 W left the battery while the output averaged 19.5 W (12.4 W over the whole
    # run; about 17 W with the display off). 15 W is between the two.
    OVERHEAD_WATTS: ClassVar[float] = 15.0

    def __init__(self, *, smoothing_half_life_minutes: float | None = None) -> None:
        self._half_life = smoothing_half_life_minutes
        self._watts: float | None = None
        self._at: float | None = None

    @property
    def watts(self) -> float | None:
        """The (smoothed) output watts as of the latest reading; None before the first."""
        return self._watts

    def update(self, reading: UpsReading) -> float:
        """Add a reading; the minutes left at the (smoothed) draw."""
        if self._half_life is None or self._watts is None or self._at is None:
            self._watts = reading.output_watts
        else:
            keep = 0.5 ** ((reading.minutes - self._at) / self._half_life)
            self._watts = keep * self._watts + (1 - keep) * reading.output_watts
        self._at = reading.minutes
        return reading.battery_pct / 100 * self.CAPACITY_WH / (self._watts + self.OVERHEAD_WATTS) * 60


class KalmanEstimate:
    """Two-state Kalman filter: energy left (Wh) and the inverter's own draw (W).

    Between readings the energy falls by (output watts + overhead) x time (it reacts to a change in draw at once);
    each battery % reading corrects it, and the gap between the two teaches the overhead. Minutes left = energy /
    (smoothed output + overhead). Its settings are fixed from the 10/12 drill: the overhead starts at 15 W (+- 5 W)
    and drifts slowly; a battery % reading is trusted to about 1% (it is reported in whole percent).
    """

    CAPACITY_WH: ClassVar[float] = WattsEstimate.CAPACITY_WH
    _INITIAL_OVERHEAD_WATTS: ClassVar[float] = 15.0
    _INITIAL_OVERHEAD_VARIANCE: ClassVar[float] = 25.0  # (5 W)^2
    _OVERHEAD_DRIFT_PER_MINUTE: ClassVar[float] = 0.05  # W^2 per minute
    _ENERGY_DRIFT_PER_MINUTE: ClassVar[float] = 0.01  # Wh^2 per minute
    _WATTS_SMOOTHING_HALF_LIFE_MINUTES: ClassVar[float] = 1.0

    def __init__(self) -> None:
        self._energy: float | None = None
        self._overhead = self._INITIAL_OVERHEAD_WATTS
        one_pct = self.CAPACITY_WH / 100
        self._p = [[one_pct ** 2, 0.0], [0.0, self._INITIAL_OVERHEAD_VARIANCE]]
        self._noise = 4 * one_pct ** 2 / 12  # whole-percent rounding (variance 1/12 of a step), with slack
        self._watts = WattsEstimate(smoothing_half_life_minutes=self._WATTS_SMOOTHING_HALF_LIFE_MINUTES)
        self._last: UpsReading | None = None

    @property
    def overhead_watts(self) -> float:
        """The inverter's own draw as learned so far."""
        return self._overhead

    def update(self, reading: UpsReading) -> float:
        """Add a reading; the minutes left."""
        self._watts.update(reading)
        smoothed = self._watts.watts if self._watts.watts is not None else reading.output_watts
        if self._energy is None or self._last is None:
            self._energy = reading.battery_pct / 100 * self.CAPACITY_WH
        else:
            dt = reading.minutes - self._last.minutes
            a = -dt / 60  # Wh per W of draw over dt
            # Predict: the draw since the last reading is the output then plus the overhead.
            self._energy += a * (self._last.output_watts + self._overhead)
            (p00, p01), (_, p11) = self._p
            self._p = [[p00 + 2 * a * p01 + a * a * p11 + self._ENERGY_DRIFT_PER_MINUTE * dt, p01 + a * p11],
                       [p01 + a * p11, p11 + self._OVERHEAD_DRIFT_PER_MINUTE * dt]]
            # Correct with the battery %.
            (p00, p01), (_, p11) = self._p
            gain_energy, gain_overhead = p00 / (p00 + self._noise), p01 / (p00 + self._noise)
            surprise = reading.battery_pct / 100 * self.CAPACITY_WH - self._energy
            self._energy += gain_energy * surprise
            self._overhead += gain_overhead * surprise
            self._p = [[(1 - gain_energy) * p00, (1 - gain_energy) * p01],
                       [p01 - gain_overhead * p00, p11 - gain_overhead * p01]]
        self._last = reading
        draw = smoothed + self._overhead
        return max(self._energy, 0.0) / draw * 60 if draw > 0 else math.inf


class SideBySide:
    """Every candidate, fed the same readings; `update` returns {column: minutes left or None}."""

    def __init__(self) -> None:
        self._estimators: dict[str, RegressionEstimate | WattsEstimate | KalmanEstimate] = {
            "regression_30min": RegressionEstimate(),
            "weighted_5min": RegressionEstimate(half_life_minutes=5.0),
            "weighted_2_5min": RegressionEstimate(half_life_minutes=2.5),
            "watts_raw": WattsEstimate(),
            "watts_smoothed": WattsEstimate(smoothing_half_life_minutes=1.0),
        }
        self._kalman = KalmanEstimate()

    COLUMNS: ClassVar[tuple[str, ...]] = (
        "regression_30min", "weighted_5min", "weighted_2_5min", "watts_raw", "watts_smoothed",
        "blend_weighted_5min", "blend_weighted_2_5min", "kalman", "kalman_overhead_watts",
    )

    def update(self, reading: UpsReading) -> dict[str, float | None]:
        out: dict[str, float | None] = {name: est.update(reading) for name, est in self._estimators.items()}
        for name in ("weighted_5min", "weighted_2_5min"):
            regression, watts = out[name], out["watts_smoothed"]
            out[f"blend_{name}"] = None if regression is None or watts is None else (regression + watts) / 2
        out["kalman"] = self._kalman.update(reading)
        out["kalman_overhead_watts"] = self._kalman.overhead_watts
        return {column: out[column] for column in self.COLUMNS}
