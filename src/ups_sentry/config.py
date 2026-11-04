"""Settings, in layers: built-in defaults, then the model's profile, then the config file, then --set on the command line.

An unknown section or key is an error, not ignored: a misspelled `overhead_wats` must not silently leave the estimate on
the profile's number.
"""
from __future__ import annotations

import copy
import tomllib
from importlib import resources
from pathlib import Path

DEFAULT_CONFIG_PATH = "~/.config/ups-sentry/config.toml"

_DEFAULTS: dict[str, dict] = {
    "device": {"driver": "bluetti", "model": None, "address": None, "scan_seconds": 20.0, "eco_registers": None},
    "estimator": {"capacity_wh": None, "overhead_watts": None, "drain_window_minutes": 30.0},
    "monitor": {"poll_interval_seconds": 120.0, "consecutive_reads_to_confirm_transition": 3,
                "min_battery_pct_drop_to_confirm_outage": 2.0, "failures_to_page_unreachable": 3,
                "eco_auto_off": False},
    "status": {"path": "~/.local/state/ups-sentry/status.json"},
    "log": {"path": None},
    "alerts": {"command": None, "pushover_api_token": None, "pushover_user_key": None},
    "heartbeat": {"url": None, "interval_seconds": 60.0},
}


class ConfigError(ValueError):
    """The config file or a --set can't be used."""


def load_profiles() -> dict[str, dict[str, dict]]:
    """Every model profile, as {driver: {model: profile}}."""
    text = resources.files("ups_sentry").joinpath("profiles.toml").read_text()
    return tomllib.loads(text)


def _merge(settings: dict, layer: dict, origin: str) -> None:
    for section, values in layer.items():
        if section not in settings or not isinstance(values, dict):
            raise ConfigError(f"{origin}: unknown section {section!r}; sections are {sorted(settings)}")
        for key, value in values.items():
            if key not in settings[section]:
                raise ConfigError(f"{origin}: unknown key {section}.{key}; keys are {sorted(settings[section])}")
            settings[section][key] = value


def _parse_set(assignment: str) -> dict:
    """"section.key=value" as a layer; the value is read as TOML, or as plain text if it isn't."""
    name, separator, text = assignment.partition("=")
    section, dot, key = name.strip().partition(".")
    if not separator or not dot:
        raise ConfigError(f"--set {assignment!r}: expected section.key=value")
    try:
        value = tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        value = text
    return {section: {key: value}}


def load_settings(path: str | None = None, overrides: list[str] | tuple[str, ...] = ()) -> dict[str, dict]:
    """Resolve the settings.

    Parameters
    ----------
    path : str, optional
        The config file. Default `DEFAULT_CONFIG_PATH`, which may be absent; a path given explicitly must exist.
    overrides : sequence of str, optional
        "section.key=value" assignments, applied last.

    Raises
    ------
    ConfigError
        On a missing explicit file, invalid TOML, an unknown section or key, or a malformed override.
    """
    file = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    layer: dict = {}
    if file.exists():
        try:
            layer = tomllib.loads(file.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{file}: {exc}") from exc
    elif path:
        raise ConfigError(f"No config file at {file}")

    settings = copy.deepcopy(_DEFAULTS)
    parsed = [_parse_set(assignment) for assignment in overrides]
    # The profile sits under the file, but which profile depends on the file and the overrides, so resolve those first.
    probe = copy.deepcopy(_DEFAULTS)
    _merge(probe, layer, str(file))
    for assignment, override in zip(overrides, parsed):
        _merge(probe, override, f"--set {assignment}")

    profile = load_profiles().get(probe["device"]["driver"], {}).get(probe["device"]["model"], {})
    for key in ("capacity_wh", "overhead_watts"):
        if key in profile:
            settings["estimator"][key] = profile[key]
    if "eco_registers" in profile:
        settings["device"]["eco_registers"] = profile["eco_registers"]

    _merge(settings, layer, str(file))
    for assignment, override in zip(overrides, parsed):
        _merge(settings, override, f"--set {assignment}")
    return settings
