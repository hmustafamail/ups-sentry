"""One-off manual logger for the Mac's own estimated power draw + the Bluetti's AC I/O/battery.

For investigating AC ECO cutoffs: samples `powermetrics`' package-power estimate and the
Bluetti's live AC input/output wattage and battery % on a timer and appends
them to a CSV, so a period of the Bluetti's AC-ECO mode cutting output can be
lined up against what the Mac was actually drawing (and what the Bluetti
itself measured) at the time.

IMPORTANT CAVEAT: `powermetrics` reports the SoC's own estimated draw (CPU +
GPU + package overhead) -- not the Mac's total AC draw at the wall, which is
closer to what `bluetti_output_w` below actually measures. Display
backlight, SSD, RAM, fans, any USB-powered peripherals, and the power
supply's own AC/DC conversion losses are all excluded from `mac_power_w`, so
it always understates the real wall wattage. Useful for spotting *trends*
and *relative* lows (e.g. what the number looks like right before/after an
AC-ECO cutoff), not for reading off an absolute wall-watts figure.

On hardware without direct energy sensors (older Intel Macs without RAPL
support, e.g. iMac19,2), `powermetrics` has no separate CPU/GPU numbers at
all -- only one combined, software-*estimated* figure ("Intel energy model
derived package power"), not a sensor reading. `mac_power_w` is sourced from
whichever of the two `powermetrics` ever prints on this machine (only the
energy-model fallback, in practice) and converted to Watts either way.
Doubly so a relative/trend signal only on this hardware, not an absolute
one.

Must be run with sudo from a real interactive Terminal window: `powermetrics` requires root, and macOS only lets a process
started there use Bluetooth. Only one program can talk to the station at a time, so running this beside the monitor starves
both (see docs/history.md).

Usage:
    sudo python scripts/power_draw_logger.py ADDRESS MODEL [--interval-seconds 30] [--out mac_power_draw.csv]

Stop with Ctrl-C; the CSV is flushed after every row. A failed read of the station leaves that row's station columns blank
rather than stopping the logger, and the reason goes to `mac_power_draw.debug.log`.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.bluetooth.device_recognizer import recognize_device
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.device_builder import build_device

_PACKAGE_POWER_LINE_RE = re.compile(r"^Package Power:\s*([\d.]+)\s*mW$")
# Older Macs (no direct RAPL/energy-sensor support -- e.g. iMac19,2) have no
# "Package Power:" line at all; powermetrics instead falls back to one
# combined, software-estimated figure in *Watts*, not milliwatts.
_ENERGY_MODEL_LINE_RE = re.compile(
    r"^Intel energy model derived package power \([^)]*\):\s*([\d.]+)W$"
)
_CSV_COLUMNS = ("timestamp_utc", "mac_power_w", "bluetti_input_w", "bluetti_output_w", "bluetti_battery_pct")
_DEBUG_LOG_PATH = Path("mac_power_draw.debug.log")


def _log_debug(message: str) -> None:
    _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DEBUG_LOG_PATH.open("a") as debug_log:
        debug_log.write(f"--- {datetime.now(timezone.utc).isoformat()}: {message} ---\n")


def _sample_mac_power_w() -> float | None:
    """Run a single 1-second `powermetrics` sample and parse the package power field.

    Returns
    -------
    float | None
        Watts, or None if `powermetrics` printed neither the "Package
        Power:" summary line nor the energy-model fallback line (logged to
        `_DEBUG_LOG_PATH` for inspection when this happens).

    Notes
    -----
    `-a 1` (poweravg every 1 sample) is required here: `powermetrics`
    only prints the Package "Power:" summary line every `-a` samples,
    which defaults to 10 -- with `-n 1` (one sample per invocation),
    sample #1 is never a multiple of that default, so the summary line
    this depends on would otherwise never print at all, silently
    producing an empty field on every single row forever.
    """
    result = subprocess.run(
        ["powermetrics", "--samplers", "cpu_power", "-n", "1", "-i", "1000", "-a", "1"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        stripped = line.strip()
        match = _PACKAGE_POWER_LINE_RE.match(stripped)
        if match:
            return float(match.group(1)) / 1000
        energy_model_match = _ENERGY_MODEL_LINE_RE.match(stripped)
        if energy_model_match:
            return float(energy_model_match.group(1))

    _log_debug("no power fields matched:\n" + result.stdout)
    return None


async def _async_sample_bluetti_power_w(
    mac_address: str, device_type: str
) -> tuple[float | None, float | None, float | None]:
    """Connect over BLE and read the Bluetti's live AC input/output wattage and battery %.

    Parameters
    ----------
    mac_address : str
        BLE MAC/UUID address of the power station.
    device_type : str
        bluetti-bt-lib device type string (e.g. "EL10").

    Returns
    -------
    tuple[float | None, float | None, float | None]
        (ac_input_w, ac_output_w, battery_pct). Any may be None if the
        device couldn't be recognized/read, or if a field was absent
        from the response -- see `_DEBUG_LOG_PATH` for the specific
        reason in that case.
    """
    recognized = await recognize_device(mac_address, asyncio.Future)
    if recognized is None:
        _log_debug(f"Could not recognize Bluetti device at {mac_address}")
        return None, None, None

    # bluetti-bt-lib's build_device only inspects the model-name prefix --
    # the trailing digits are the device's advertised serial suffix and
    # are irrelevant here.
    device = build_device(device_type + "00000000")
    if device is None:
        _log_debug(f"bluetti-bt-lib has no device class for '{device_type}'")
        return None, None, None

    reader = DeviceReader(
        mac_address,
        device,
        asyncio.Future,
        DeviceReaderConfig(use_encryption=recognized.encrypted),
    )
    data = await reader.read()
    if data is None:
        _log_debug(f"BLE read failed for {mac_address}")
        return None, None, None

    return (
        data.get(FieldName.AC_INPUT_POWER.value),
        data.get(FieldName.AC_OUTPUT_POWER.value),
        data.get(FieldName.BATTERY_SOC.value),
    )


def _sample_bluetti_power_w(
    mac_address: str, device_type: str
) -> tuple[float | None, float | None, float | None]:
    """Sync wrapper around `_async_sample_bluetti_power_w` that swallows BLE failures.

    A single flaky BLE read shouldn't take down a logging run that might
    be left running for hours to catch an outage -- failures are logged to
    `_DEBUG_LOG_PATH` (either from within `_async_sample_bluetti_power_w`,
    for an expected/recognized failure mode, or here, for anything else
    that raised) and reported as `(None, None, None)` instead of raising.
    """
    try:
        return asyncio.run(_async_sample_bluetti_power_w(mac_address, device_type))
    except Exception as exc:
        _log_debug(f"Bluetti BLE read failed: {exc}")
        return None, None, None


def _log_forever(mac_address: str, device_type: str, interval_seconds: float, out_path: Path) -> None:
    """Append one power sample per `interval_seconds` to `out_path` until interrupted."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists()

    with out_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(_CSV_COLUMNS)
        print(f"Logging to {out_path} every {interval_seconds:.0f}s. Ctrl-C to stop.")
        while True:
            mac_power_w = _sample_mac_power_w()
            bluetti_input_w, bluetti_output_w, bluetti_battery_pct = _sample_bluetti_power_w(
                mac_address, device_type
            )
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(),
                    mac_power_w if mac_power_w is not None else "",
                    bluetti_input_w if bluetti_input_w is not None else "",
                    bluetti_output_w if bluetti_output_w is not None else "",
                    bluetti_battery_pct if bluetti_battery_pct is not None else "",
                ]
            )
            f.flush()
            time.sleep(interval_seconds)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="the station's address, from `ups-sentry scan`")
    parser.add_argument("model", help="bluetti-bt-lib's device type, e.g. EL10")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--out", type=Path, default=Path("mac_power_draw.csv"))
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        _log_forever(args.address, args.model, args.interval_seconds, args.out)
    except KeyboardInterrupt:
        print("\nStopped.")
