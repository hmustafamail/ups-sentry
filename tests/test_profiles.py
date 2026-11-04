"""Checks every model profile, so a PR adding one is checked automatically. pytest tests/test_profiles.py -v"""
from __future__ import annotations

import pytest
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.device_builder import build_device

from ups_sentry.config import load_profiles

PROFILES = [(driver, model, profile) for driver, models in load_profiles().items() for model, profile in models.items()]
BLUETTI_ECO_FIELDS = {"AC": FieldName.CTRL_ECO_AC, "DC": FieldName.CTRL_ECO_DC}
KNOWN_KEYS = {"capacity_wh", "overhead_watts", "eco_registers", "source"}


@pytest.mark.parametrize("driver, model, profile", PROFILES, ids=[f"{d}.{m}" for d, m, _ in PROFILES])
class TestEveryProfile:
    def test_says_where_the_eco_switches_are(self, driver, model, profile):
        """Without it, `ups-sentry run` refuses the model, so a profile that lacks it would look supported and not be."""
        assert "eco_registers" in profile

    def test_only_known_keys(self, driver, model, profile):
        assert set(profile) <= KNOWN_KEYS

    def test_says_where_its_numbers_came_from(self, driver, model, profile):
        assert profile.get("source", "").strip()

    def test_numbers_are_plausible(self, driver, model, profile):
        assert profile.get("capacity_wh", 1) > 0
        assert profile.get("overhead_watts", 0) >= 0

    def test_capacity_and_overhead_come_together(self, driver, model, profile):
        """One without the other can't count energy, so it would look configured and do nothing."""
        assert ("capacity_wh" in profile) == ("overhead_watts" in profile)

    def test_eco_registers_match_the_drivers_library(self, driver, model, profile):
        """Naive version: a typo here would stop every write, or, without this check, go unnoticed until an outage."""
        if driver != "bluetti" or "eco_registers" not in profile:
            pytest.skip("no ECO registers to check against bluetti-bt-lib")
        device = build_device(model + "00000000")
        assert device is not None, f"bluetti-bt-lib has no device class for {model}"
        addresses = {field.name: field.address for field in device.fields}
        for output, register in profile["eco_registers"].items():
            assert addresses.get(BLUETTI_ECO_FIELDS[output].value) == register
