"""The dead-man's switch: pings an outside service (e.g. healthchecks.io) while the monitor's loop keeps running.

If the pings stop (no internet, no power, a crash, a hung loop), the outside service raises the alarm. An alert can't
come from a machine that has lost its connection, so only an outside service can report that.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import ClassVar

from ups_sentry.alerts import Alerts

log = logging.getLogger(__name__)


def _http_get(url: str, timeout: float) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - the configured ping URL
        response.read()


class Heartbeat:
    """Pings `url` every `interval_seconds` while `beat()` has been called within `stale_after_seconds`.

    The main loop calls `beat()` once per pass; a daemon thread does the network calls, so a dead connection never
    stalls the loop. A ping that can't reach the service stays quiet, since that silence is what the service alerts on.
    A ping the service rejects (a 4xx: a wrong URL) alerts once: a check that never received a ping never fires, so a
    wrong URL would otherwise fail silently.
    """

    _TIMEOUT_SECONDS: ClassVar[float] = 10.0

    def __init__(self, url: str, alerts: Alerts, *, interval_seconds: float = 60.0, stale_after_seconds: float = 300.0,
                 get_fn: Callable[[str, float], None] = _http_get, clock: Callable[[], float] = time.monotonic) -> None:
        self._url = url
        self._alerts = alerts
        self._interval = interval_seconds
        self._stale_after = stale_after_seconds
        self._get = get_fn
        self._clock = clock
        self._last_beat = clock()
        self._stalled_logged = False
        self._rejected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """Record that the main loop just completed a pass."""
        self._last_beat = self._clock()

    def tick(self) -> None:
        """One check: ping if the loop is alive. Called by the thread every interval."""
        if self._clock() - self._last_beat > self._stale_after:
            if not self._stalled_logged:
                self._stalled_logged = True
                log.error("The main loop hasn't finished a pass in %.0f min; pings stopped.", self._stale_after / 60)
            return
        self._stalled_logged = False
        try:
            self._get(self._url, self._TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and not self._rejected:
                self._rejected = True
                self._alerts.alert("Dead-man's switch URL rejected",
                                   f"The monitoring service rejected the ping URL ({exc}). Until heartbeat.url is "
                                   "fixed, nothing will alert if this process stops.")
            else:
                log.info("Heartbeat ping failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - no internet is exactly when this fails; the silence is the alarm
            log.info("Heartbeat ping failed: %s", exc)
        else:
            self._rejected = False

    def start(self) -> Heartbeat:
        """Start the daemon thread (it dies with the process). Returns self."""
        def run() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - the thread must never die
                    log.error("Heartbeat error: %s", exc)
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=run, name="heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the thread (for tests and a clean shutdown)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
