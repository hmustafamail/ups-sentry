"""Tests for src/ups_sentry/alerts.py. pytest tests/test_alerts.py -v"""
from __future__ import annotations

import logging
import sys
import urllib.parse

import pytest

from ups_sentry.alerts import AlertFanout, CommandAlerts, PushoverAlerts, PushoverRejected


def pushover(post, sleeps=None):
    return PushoverAlerts("token", "user", post_fn=post, sleep_fn=(sleeps if sleeps is not None else []).append)


class TestPushoverAlerts:
    @pytest.mark.parametrize("priority, number", [("normal", "0"), ("high", "1"), ("emergency", "2")])
    def test_priority(self, priority, number):
        sent = []
        pushover(sent.append).alert("Title", "Body", priority)
        body = urllib.parse.parse_qs(sent[0].decode())
        assert body["priority"] == [number] and body["title"] == ["Title"] and body["message"] == ["Body"]
        assert ("retry" in body) == (priority == "emergency") and ("expire" in body) == (priority == "emergency")

    def test_a_rejected_request_is_not_retried(self):
        calls = []

        def post(data):
            calls.append(data)
            raise PushoverRejected("bad token")

        with pytest.raises(PushoverRejected):
            pushover(post).alert("T", "B")
        assert len(calls) == 1

    def test_temporary_failures_retry_then_succeed(self):
        attempts, sleeps = [], []

        def post(data):
            attempts.append(data)
            if len(attempts) < 3:
                raise OSError("connection reset")

        pushover(post, sleeps).alert("T", "B")
        assert len(attempts) == 3 and sleeps == [5.0, 5.0]

    def test_gives_up_in_bounded_time(self):
        attempts = []

        def post(data):
            attempts.append(data)
            raise OSError("down")

        with pytest.raises(OSError):
            pushover(post).alert("T", "B")
        assert len(attempts) == 3


class TestCommandAlerts:
    def test_passes_priority_title_and_message(self, tmp_path):
        out = tmp_path / "out.txt"
        script = "import sys; open(sys.argv[1], 'w').write('|'.join(sys.argv[2:]))"
        CommandAlerts([sys.executable, "-c", script, str(out)]).alert("Title", "Body", "high")
        assert out.read_text() == "high|Title|Body"


class FakeChannel:
    def __init__(self, error=None):
        self.error, self.alerts = error, []

    def alert(self, title, message, priority="normal"):
        self.alerts.append((title, message, priority))
        if self.error:
            raise self.error


class TestAlertFanout:
    def test_a_failing_channel_is_logged_and_the_others_still_get_it(self, caplog):
        """An alert must never stop the monitor: the outage alert goes out when things are already going wrong."""
        broken, working = FakeChannel(OSError("no network")), FakeChannel()
        with caplog.at_level(logging.WARNING):
            AlertFanout([broken, working]).alert("Power outage", "Body", "high")
        assert working.alerts == [("Power outage", "Body", "high")]
        assert "no network" in caplog.text

    def test_logs_with_no_channels(self, caplog):
        with caplog.at_level(logging.INFO):
            AlertFanout().alert("Power outage", "Body")
        assert "Power outage" in caplog.text
