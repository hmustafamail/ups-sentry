"""Tests for src/ups_sentry/heartbeat.py. pytest tests/test_heartbeat.py -v"""
from __future__ import annotations

import urllib.error

from ups_sentry.heartbeat import Heartbeat


class FakeAlerts:
    def __init__(self):
        self.titles = []

    def alert(self, title, message, priority="normal"):
        self.titles.append(title)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def rejected(url, timeout):
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


def make(get):
    alerts, clock = FakeAlerts(), Clock()
    return Heartbeat("https://example.invalid/x", alerts, get_fn=get, clock=clock), alerts, clock


class TestHeartbeat:
    def test_pings_while_the_loop_is_alive(self):
        pings = []
        heartbeat, _, _ = make(lambda url, timeout: pings.append(url))
        heartbeat.tick()
        assert pings == ["https://example.invalid/x"]

    def test_stops_pinging_when_the_loop_stalls(self):
        """The silence is what makes the outside service raise the alarm."""
        pings = []
        heartbeat, _, clock = make(lambda url, timeout: pings.append(url))
        clock.now = 301.0
        heartbeat.tick()
        assert pings == []
        heartbeat.beat()
        heartbeat.tick()
        assert len(pings) == 1

    def test_a_rejected_url_alerts_once(self):
        """A check that never received a ping never fires, so a wrong URL would otherwise fail silently."""
        heartbeat, alerts, _ = make(rejected)
        heartbeat.tick()
        heartbeat.tick()
        assert alerts.titles == ["Dead-man's switch URL rejected"]

    def test_no_internet_stays_quiet(self):
        def down(url, timeout):
            raise OSError("no route to host")

        heartbeat, alerts, _ = make(down)
        heartbeat.tick()
        assert alerts.titles == []
