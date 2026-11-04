"""Where alerts go: the log, always; Pushover and a command of your own, if configured."""
from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from typing import ClassVar, Literal, Protocol

log = logging.getLogger(__name__)

Priority = Literal["normal", "high", "emergency"]


class Alerts(Protocol):
    """Told about every power event. Normal: a routine notice. High: past quiet hours, not repeated. Emergency:
    repeated until someone acknowledges it, where the channel can."""

    def alert(self, title: str, message: str, priority: Priority = "normal") -> None: ...


class PushoverRejected(RuntimeError):
    """Pushover refused the request itself (a 4xx, or a 200 whose "status" isn't 1). Per its API guidance, repeating
    the same request won't work, so it isn't retried."""


class PushoverAlerts:
    """Push notifications through the Pushover API."""

    _API_URL: ClassVar[str] = "https://api.pushover.net/1/messages.json"
    _PRIORITIES: ClassVar[dict[str, int]] = {"normal": 0, "high": 1, "emergency": 2}
    _EMERGENCY_RETRY_SECONDS: ClassVar[int] = 60
    _EMERGENCY_EXPIRE_SECONDS: ClassVar[int] = 3600
    _TIMEOUT_SECONDS: ClassVar[float] = 10.0
    _MIN_RETRY_DELAY_SECONDS: ClassVar[float] = 5.0  # Pushover's own minimum
    _MAX_ATTEMPTS: ClassVar[int] = 3  # bounded: this runs inside the monitor's loop

    def __init__(self, api_token: str, user_key: str, *, post_fn: Callable[[bytes], None] | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self._api_token = api_token
        self._user_key = user_key
        self._post = post_fn or self._default_post
        self._sleep = sleep_fn

    def _default_post(self, data: bytes) -> None:
        request = urllib.request.Request(self._API_URL, data=data)
        try:
            with urllib.request.urlopen(request, timeout=self._TIMEOUT_SECONDS) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                detail = exc.read().decode("utf-8", errors="replace")
                raise PushoverRejected(f"Pushover rejected the request (HTTP {exc.code}): {detail}") from exc
            raise
        if body.get("status") != 1:
            raise PushoverRejected(f"Pushover rejected the request: {body.get('errors', body)}")

    def alert(self, title: str, message: str, priority: Priority = "normal") -> None:
        """Send it, retrying temporary failures.

        Raises
        ------
        PushoverRejected
            At once if the request itself is invalid.
        Exception
            Whatever the last attempt raised, once `_MAX_ATTEMPTS` temporary failures are used up.
        """
        payload: dict[str, str | int] = {"token": self._api_token, "user": self._user_key, "title": title,
                                         "message": message, "priority": self._PRIORITIES[priority]}
        if priority == "emergency":
            payload["retry"] = self._EMERGENCY_RETRY_SECONDS
            payload["expire"] = self._EMERGENCY_EXPIRE_SECONDS
        data = urllib.parse.urlencode(payload).encode("utf-8")
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                self._post(data)
                return
            except PushoverRejected:
                raise
            except Exception:
                if attempt == self._MAX_ATTEMPTS:
                    raise
                self._sleep(self._MIN_RETRY_DELAY_SECONDS)


class CommandAlerts:
    """Runs your command with the priority, title and message as its last three arguments."""

    _TIMEOUT_SECONDS: ClassVar[float] = 30.0

    def __init__(self, command: Sequence[str]) -> None:
        self._command = list(command)

    def alert(self, title: str, message: str, priority: Priority = "normal") -> None:
        subprocess.run([*self._command, priority, title, message], check=True, timeout=self._TIMEOUT_SECONDS)


class AlertFanout:
    """Logs every alert and tells each channel. A failing channel is logged, never raised: an alert must not stop the
    monitor, and the other channels still get it."""

    def __init__(self, channels: Sequence[Alerts] = ()) -> None:
        self._channels = list(channels)

    def alert(self, title: str, message: str, priority: Priority = "normal") -> None:
        log.log(logging.INFO if priority == "normal" else logging.WARNING, "[%s] %s: %s", priority, title, message)
        for channel in self._channels:
            try:
                channel.alert(title, message, priority)
            except Exception as exc:  # noqa: BLE001 - see the class docstring
                log.error("Alert %r didn't go out through %s: %s", title, type(channel).__name__, exc)
