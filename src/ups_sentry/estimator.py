"""Minutes left on battery. No Bluetooth, clock or files: readings in, an estimate out.

Battery percentages lag the energy and fall fast near empty, so the estimate counts energy instead: the first on-battery
reading's percentage of the capacity, then minus (output watts + the station's own draw) x time, never looking at the
percentage again. Minutes left is the lower of two:

- the counted energy over the slope of a least-squares line through the last `drain_window_minutes` of it (slow, ignores
  one-reading spikes);
- the counted energy over the 3-minute smoothed output plus the station's own draw (sees a load rise within minutes).

Without a capacity and an overhead, or without output watts from the station, it falls back to the same line through the
battery percentage, which reads longer near empty.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar


class RuntimeEstimator:
    """Feed it every reading on battery with `update`; `reset` on wall power; ask `minutes_left`."""

    _MIN_DRAIN_SPAN_MINUTES: ClassVar[float] = 4.0
    _WATTS_SMOOTHING_HALF_LIFE_MINUTES: ClassVar[float] = 3.0

    def __init__(self, *, capacity_wh: float | None = None, overhead_watts: float | None = None,
                 drain_window_minutes: float = 30.0) -> None:
        """Set the station's energy and its own draw.

        Parameters
        ----------
        capacity_wh : float, optional
            Usable energy when full. Needed, with `overhead_watts`, to count energy.
        overhead_watts : float, optional
            The station's own draw on top of its output. Needed, with `capacity_wh`, to count energy.
        drain_window_minutes : float, optional
            How far back the drain-rate line looks. Default 30.

        Raises
        ------
        ValueError
            If `capacity_wh` isn't positive, or `overhead_watts` is negative.
        """
        if capacity_wh is not None and capacity_wh <= 0:
            raise ValueError(f"capacity_wh must be > 0, was {capacity_wh}")
        if overhead_watts is not None and overhead_watts < 0:
            raise ValueError(f"overhead_watts must be >= 0, was {overhead_watts}")
        self._capacity_wh = capacity_wh
        self._overhead_watts = overhead_watts
        self._drain_window = timedelta(minutes=drain_window_minutes)
        self.reset()

    @property
    def counts_energy(self) -> bool:
        """Whether it has the capacity and overhead that counting energy needs."""
        return self._capacity_wh is not None and self._overhead_watts is not None

    @property
    def counted_wh(self) -> float | None:
        """The energy counted so far on this outage; None when not counting."""
        return self._counted_wh

    def reset(self) -> None:
        """Back on wall power: forget the outage."""
        self._drain_readings: list[tuple[datetime, float]] = []
        self._counted_readings: list[tuple[datetime, float]] = []
        self._counted_wh: float | None = None
        self._last_read: tuple[datetime, float | None] | None = None  # when, output watts
        self._smoothed_watts: float | None = None
        self._battery_pct: float | None = None

    def update(self, now: datetime, battery_pct: float, output_watts: float | None) -> None:
        """Add one on-battery reading."""
        self._battery_pct = battery_pct
        self._drain_readings.append((now, battery_pct))
        self._drain_readings = [(when, pct) for when, pct in self._drain_readings
                                if now - when <= self._drain_window]
        if self.counts_energy:
            self._count_energy(now, battery_pct, output_watts)

    def minutes_left(self) -> float | None:
        """Minutes until empty; None while there is too little to go on or the battery isn't falling."""
        if self._battery_pct is None:
            return None
        if self._counted_wh is None or self._smoothed_watts is None:
            rate = self._drain_rate(self._drain_readings)  # % per minute
            return None if rate is None else round(self._battery_pct / rate, 1)
        energy = max(self._counted_wh, 0.0)
        estimates = [energy / ((self._smoothed_watts + self._overhead_watts) / 60)]
        rate = self._drain_rate(self._counted_readings)  # Wh per minute
        if rate is not None:
            estimates.append(energy / rate)
        return round(min(estimates), 1)

    def _drain_rate(self, readings: list[tuple[datetime, float]]) -> float | None:
        """The fall per minute of a least-squares line through `readings`; None if they span under the minimum or
        aren't falling."""
        if len(readings) < 2:
            return None
        start = readings[0][0]
        xs = [(when - start).total_seconds() / 60 for when, _ in readings]
        ys = [value for _, value in readings]
        if xs[-1] - xs[0] < self._MIN_DRAIN_SPAN_MINUTES:
            return None
        x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
        spread = sum((x - x_mean) ** 2 for x in xs)
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / spread
        return -slope if slope < 0 else None

    def _count_energy(self, now: datetime, battery_pct: float, output_watts: float | None) -> None:
        last = self._last_read
        if last is None:
            if output_watts is not None:
                self._counted_wh = battery_pct / 100 * self._capacity_wh
                self._smoothed_watts = output_watts
            self._last_read = (now, output_watts)
        elif self._counted_wh is None or self._smoothed_watts is None:
            return  # no output watts when the outage began: the battery % estimate stands
        else:
            minutes = (now - last[0]).total_seconds() / 60
            drawn = last[1] if last[1] is not None else self._smoothed_watts
            self._counted_wh -= (drawn + self._overhead_watts) * minutes / 60
            if output_watts is not None:
                keep = 0.5 ** (minutes / self._WATTS_SMOOTHING_HALF_LIFE_MINUTES)
                self._smoothed_watts = keep * self._smoothed_watts + (1 - keep) * output_watts
            self._last_read = (now, output_watts)
        if self._counted_wh is not None:
            self._counted_readings.append((now, self._counted_wh))
            self._counted_readings = [(when, wh) for when, wh in self._counted_readings
                                      if now - when <= self._drain_window]
