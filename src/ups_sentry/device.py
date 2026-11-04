"""The vendor-neutral view of a power station: what every driver returns, and the interfaces drivers implement."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DeviceState:
    """One point-in-time reading.

    Attributes
    ----------
    battery_pct : float
        State of charge, 0-100.
    ac_input_present : bool
        Whether the station is receiving wall power.
    auto_off : frozenset of str, or None
        The outputs ("AC", "DC") set to switch off by themselves at low load (ECO modes); empty when none is. None
        when the read didn't include the settings.
    fields : dict
        Everything the station reported, by the vendor's field names, as JSON-friendly values. For the log and the
        status file; nothing decides on these.
    output_watts : float or None
        The power going out on all outputs; None when the read didn't include it.
    vendor_minutes_left : float or None
        The station's own estimate of minutes left, for comparison only.
    """

    battery_pct: float
    ac_input_present: bool
    auto_off: frozenset[str] | None = None
    fields: dict[str, object] = field(default_factory=dict)
    output_watts: float | None = None
    vendor_minutes_left: float | None = None


class DeviceReadError(Exception):
    """A read failed, whatever the transport or vendor reason."""


class DeviceWriteError(Exception):
    """A change to the station's settings couldn't be made or confirmed."""


class SupportsRead(Protocol):
    """What a driver must do."""

    def read_state(self) -> DeviceState: ...


@runtime_checkable
class SupportsEcoOff(Protocol):
    """Optional: a driver that can switch auto-off (ECO) modes off. The monitor only alerts for one without it."""

    def switch_eco_off(self, outputs: frozenset[str]) -> frozenset[str]: ...
