"""Tests for src/ups_sentry/cli.py. pytest tests/test_cli.py -v"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ups_sentry.cli import main
from ups_sentry.config import NoEcoProfileError
from ups_sentry.device import DeviceReadError, DeviceState
from ups_sentry.drills import measure_drain, read_log
from ups_sentry.drivers import create_device
from ups_sentry.status import read_status, write_status

FIXTURE = str(Path(__file__).parent / "fixtures" / "drain_2026-10-16.csv")
START = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[device]\nmodel = "EL10"\naddress = "A"\n[status]\npath = "{tmp_path}/status.json"\n')
    return str(path)


class TestCommands:
    def test_score_prints_the_real_drain(self, config, capsys):
        assert main(["--config", config, "score", FIXTURE]) == 0
        out = capsys.readouterr().out
        assert "3.1 min" in out and "16.5 min" in out and "-0.7 Wh" in out

    def test_set_changes_what_is_scored(self, config, capsys):
        assert main(["--config", config, "--set", "estimator.overhead_watts=30", "score", FIXTURE]) == 0
        assert "overhead 30 W" in capsys.readouterr().out

    def test_calibrate_from_one_drain_uses_the_profiles_capacity(self, config, capsys):
        assert main(["--config", config, "calibrate", FIXTURE]) == 0
        out = capsys.readouterr().out
        assert "capacity_wh = 128.0" in out and "overhead_watts = 14." in out

    def test_calibrate_with_no_capacity_says_what_to_do(self, tmp_path, capsys):
        path = tmp_path / "c.toml"
        path.write_text('[device]\nmodel = "AC999"\n')
        assert main(["--config", str(path), "calibrate", FIXTURE]) == 1
        assert "--capacity-wh" in capsys.readouterr().err

    def test_calibrate_refuses_three_logs(self, config, capsys):
        assert main(["--config", config, "calibrate", FIXTURE, FIXTURE, FIXTURE]) == 2

    def test_status_prints_the_file(self, config, tmp_path, capsys):
        write_status(tmp_path / "status.json", {"power": "wall"})
        assert main(["--config", config, "status"]) == 0
        assert '"power": "wall"' in capsys.readouterr().out

    def test_status_without_a_file_fails_clearly(self, config, capsys):
        assert main(["--config", config, "status"]) == 1
        assert "is the monitor running" in capsys.readouterr().err

    def test_a_config_mistake_is_a_message_not_a_traceback(self, tmp_path, capsys):
        path = tmp_path / "c.toml"
        path.write_text("[estimator]\noverhead_wats = 18\n")
        assert main(["--config", str(path), "status"]) == 1
        assert "overhead_wats" in capsys.readouterr().err

    def test_eco_off_writes_nothing_unless_confirmed(self, config, mocker, capsys):
        write = mocker.patch("ups_sentry.drivers.bluetti.BluettiDevice.switch_eco_off")
        mocker.patch("builtins.input", return_value="no")
        assert main(["--config", config, "eco-off"]) == 0
        write.assert_not_called()

    def test_eco_off_refuses_an_unknown_output(self, config, mocker):
        write = mocker.patch("ups_sentry.drivers.bluetti.BluettiDevice.switch_eco_off")
        assert main(["--config", config, "eco-off", "USB", "--yes"]) == 2
        write.assert_not_called()

    def test_eco_off_writes_both_when_confirmed(self, config, mocker):
        write = mocker.patch("ups_sentry.drivers.bluetti.BluettiDevice.switch_eco_off", return_value=frozenset())
        mocker.patch("builtins.input", return_value="yes")
        assert main(["--config", config, "eco-off"]) == 0
        write.assert_called_once_with(frozenset({"AC", "DC"}))


class FakeDevice:
    def __init__(self, states):
        self._states = list(states)

    def read_state(self):
        if not self._states:
            raise KeyboardInterrupt
        state = self._states.pop(0)
        if isinstance(state, Exception):
            raise state
        return state


class TestLoops:
    """`run` and `drill` need real hardware to do anything useful, so these use a fake one and stop it with Ctrl-C."""

    def test_run_writes_the_status_file_from_the_settings(self, config, tmp_path, mocker):
        device = FakeDevice([DeviceState(battery_pct=80.0, ac_input_present=True)])
        mocker.patch("ups_sentry.cli.create_device", return_value=device)
        mocker.patch("ups_sentry.cli.time.sleep")
        ticks = iter([START, START + timedelta(hours=1)])
        clock = mocker.patch("ups_sentry.cli.datetime")
        clock.now.side_effect = lambda tz: next(ticks)
        assert main(["--config", config, "run"]) == 0
        status = read_status(tmp_path / "status.json")
        assert status["power"] == "wall" and status["battery_pct"] == 80.0 and status["schema_version"] == 1

    def test_run_switches_eco_off_by_default_on_a_profiled_model(self, config, mocker):
        monitor = mocker.patch("ups_sentry.cli.Monitor")
        monitor.return_value.maybe_poll_and_act.side_effect = KeyboardInterrupt
        mocker.patch("ups_sentry.cli.create_device")
        assert main(["--config", config, "run"]) == 0
        assert monitor.call_args.kwargs["eco_auto_off"] is True

    def test_run_logs_and_raises_on_a_model_without_a_profile(self, tmp_path, mocker, caplog):
        """Naive version: printing a message and returning 1 is what other errors do, but this one must also reach the log
        file, since nobody may be watching the terminal."""
        path = tmp_path / "c.toml"
        path.write_text('[device]\nmodel = "AC999"\naddress = "A"\n')
        create = mocker.patch("ups_sentry.cli.create_device")
        with caplog.at_level(logging.ERROR), pytest.raises(NoEcoProfileError, match="AC999"):
            main(["--config", str(path), "run"])
        assert "AC999" in caplog.text
        create.assert_not_called()

    def test_other_commands_still_work_without_a_profile(self, tmp_path, capsys):
        path = tmp_path / "c.toml"
        path.write_text('[device]\nmodel = "AC999"\n')
        assert main(["--config", str(path), "score", FIXTURE]) == 0

    def test_run_alerts_and_reraises_on_a_crash(self, config, mocker):
        device = FakeDevice([RuntimeError("boom")])
        mocker.patch("ups_sentry.cli.create_device", return_value=device)
        alert = mocker.patch("ups_sentry.alerts.AlertFanout.alert")
        with pytest.raises(RuntimeError):
            main(["--config", config, "run"])
        assert alert.call_args.args[0] == "UPS monitor crashed"

    def test_drill_log_can_be_read_back_and_measured(self, config, tmp_path, mocker):
        states = [DeviceState(battery_pct=100.0 - 45 * n, ac_input_present=False, output_watts=12.0,
                              vendor_minutes_left=300.0) for n in range(3)]
        mocker.patch("ups_sentry.cli.create_device",
                     return_value=FakeDevice([*states[:1], DeviceReadError("Timeout"), *states[1:]]))
        mocker.patch("ups_sentry.cli.time.sleep")
        log = tmp_path / "drill.csv"
        assert main(["--config", config, "drill", str(log)]) == 0
        readings = read_log(str(log))
        assert [r.battery_pct for r in readings] == [100.0, None, 55.0, 10.0]
        assert readings[1].error == "Timeout" and readings[0].vendor_minutes == 300.0
        assert measure_drain(readings).start_pct == 100.0


class TestCreateDevice:
    def test_missing_address_is_clear(self):
        with pytest.raises(ValueError, match="device.address"):
            create_device({"device": {"driver": "bluetti", "model": "EL10", "address": None}})

    def test_unknown_driver_is_clear(self):
        with pytest.raises(ValueError, match="Unknown driver"):
            create_device({"device": {"driver": "acme", "model": "X", "address": "A"}})
