"""Drivers: one module per vendor. To add one, implement `device.SupportsRead` and add it to `create_device`."""
from __future__ import annotations

from ups_sentry.device import SupportsRead


def create_device(settings: dict) -> SupportsRead:
    """The driver for `settings["device"]`, imported only when asked for.

    Raises
    ------
    ValueError
        If the driver is unknown, or the address or model is missing.
    """
    device = settings["device"]
    driver = device.get("driver")
    missing = [key for key in ("address", "model") if not device.get(key)]
    if missing:
        raise ValueError(f"Set device.{' and device.'.join(missing)} in the config")
    if driver == "bluetti":
        from ups_sentry.drivers.bluetti import BluettiDevice

        return BluettiDevice(device["address"], device["model"], eco_registers=device.get("eco_registers"),
                             scan_seconds=device.get("scan_seconds", 20.0))
    raise ValueError(f"Unknown driver {driver!r}; available: bluetti")
