"""The status file: the whole contract with anything that reads the monitor.

Fields, as of the monitor's last attempt:

schema_version : bumped when a field changes meaning or goes away; adding a field doesn't bump it.
checked_at : ISO 8601 UTC; the last attempt to read the station, failed or not. If it is old, the monitor isn't running.
last_ok_at : ISO 8601 UTC or null; when the station last answered. The fields below are as of then.
power : "wall", "battery", or "unknown" (no successful read yet). Debounced: one noisy read never flips it.
battery_pct : 0-100, or null before the first successful read.
minutes_left : on battery, minutes until empty; null on wall power, or with too little to go on.
error : the last read's error, or null when it succeeded.
consecutive_failures : failed reads since the last successful one.
station_fields : everything the station reported on its last good read, by the vendor's field names, or null.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1


def write_status(path: str | os.PathLike[str], status: dict) -> None:
    """Write `status` atomically: a reader sees the old file or the new one, never half of one."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(status, file, indent=2, default=str)  # a driver's odd field must not stop the monitor
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o644)  # mkstemp's 0600 would hide it from readers running as another user
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_status(path: str | os.PathLike[str]) -> dict | None:
    """The last status written, or None if there is none or it can't be read."""
    try:
        status = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None
