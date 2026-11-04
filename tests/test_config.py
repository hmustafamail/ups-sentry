"""Tests for src/ups_sentry/config.py. pytest tests/test_config.py -v"""
from __future__ import annotations

import pytest

from ups_sentry.config import ConfigError, NoEcoProfileError, load_settings, resolve_eco_auto_off


@pytest.fixture
def config_file(tmp_path):
    def write(text: str) -> str:
        path = tmp_path / "config.toml"
        path.write_text(text)
        return str(path)
    return write


class TestLayers:
    def test_the_profile_fills_in_a_known_model(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "EL10"\naddress = "A"\n'))
        assert settings["estimator"]["capacity_wh"] == 128.0
        assert settings["estimator"]["overhead_watts"] == 15.0
        assert settings["device"]["eco_registers"] == {"AC": 2017, "DC": 2014}

    def test_an_unknown_model_has_no_numbers(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "AC999"\n'))
        assert settings["estimator"]["capacity_wh"] is None
        assert settings["estimator"]["overhead_watts"] is None

    def test_the_file_overrides_the_profile(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "EL10"\n[estimator]\noverhead_watts = 18.5\n'))
        assert settings["estimator"]["overhead_watts"] == 18.5
        assert settings["estimator"]["capacity_wh"] == 128.0

    def test_set_overrides_the_file(self, config_file):
        path = config_file('[device]\nmodel = "EL10"\n[estimator]\noverhead_watts = 18.5\n')
        assert load_settings(path, ["estimator.overhead_watts=20"])["estimator"]["overhead_watts"] == 20

    def test_set_can_choose_the_model_whose_profile_applies(self, config_file):
        settings = load_settings(config_file(""), ["device.model=EL10"])
        assert settings["estimator"]["capacity_wh"] == 128.0

    @pytest.mark.parametrize("text, expected", [("true", True), ("3", 3), ("1.5", 1.5), ("abc", "abc")])
    def test_set_reads_toml_values_or_else_text(self, config_file, text, expected):
        assert load_settings(config_file(""), [f"monitor.poll_interval_seconds={text}"])["monitor"][
            "poll_interval_seconds"] == expected



class TestResolveEcoAutoOff:
    def test_on_for_a_model_with_a_profile(self, config_file):
        assert resolve_eco_auto_off(load_settings(config_file('[device]\nmodel = "EL10"\n'))) is True

    def test_a_model_without_a_profile_is_refused_by_name(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "AC999"\n'))
        with pytest.raises(NoEcoProfileError, match="AC999"):
            resolve_eco_auto_off(settings)

    @pytest.mark.parametrize("choice", [True, False])
    def test_an_explicit_choice_beats_the_missing_profile(self, config_file, choice):
        settings = load_settings(config_file(f'[device]\nmodel = "AC999"\n[monitor]\neco_auto_off = {str(choice).lower()}\n'))
        assert resolve_eco_auto_off(settings) is choice

    def test_an_owner_can_turn_it_off_on_a_profiled_model(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "EL10"\n[monitor]\neco_auto_off = false\n'))
        assert resolve_eco_auto_off(settings) is False

    def test_eco_registers_in_the_config_count_as_a_profile(self, config_file):
        settings = load_settings(config_file('[device]\nmodel = "AC999"\neco_registers = { AC = 1, DC = 2 }\n'))
        assert resolve_eco_auto_off(settings) is True


class TestErrors:
    @pytest.mark.parametrize("text", ['[estimator]\noverhead_wats = 18\n', '[estimater]\noverhead_watts = 18\n',
                                      'overhead_watts = 18\n'])
    def test_a_misspelling_is_an_error_not_ignored(self, config_file, text):
        """Naive version: an ignored `overhead_wats` would silently leave the estimate on the profile's number."""
        with pytest.raises(ConfigError, match="unknown"):
            load_settings(config_file(text))

    def test_a_misspelled_override_is_an_error(self, config_file):
        with pytest.raises(ConfigError, match="unknown key"):
            load_settings(config_file(""), ["estimator.overhead_wats=18"])

    @pytest.mark.parametrize("assignment", ["estimator.overhead_watts", "overhead_watts=18"])
    def test_a_malformed_override_is_an_error(self, config_file, assignment):
        with pytest.raises(ConfigError, match="expected section.key=value"):
            load_settings(config_file(""), [assignment])

    def test_invalid_toml_is_an_error(self, config_file):
        with pytest.raises(ConfigError):
            load_settings(config_file("[device\n"))

    def test_a_named_file_must_exist(self, tmp_path):
        with pytest.raises(ConfigError, match="No config file"):
            load_settings(str(tmp_path / "absent.toml"))
