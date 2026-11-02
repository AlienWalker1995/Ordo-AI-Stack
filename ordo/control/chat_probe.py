"""The GPU chat service's liveness, probed in the background and read from memory.

`GET /lease` (the model gateway's fallback gate) and `GET /metrics` both report whether the chat
engine answers its /health. Probing inline made /metrics wait up to 3 s on a loading or evicted
engine, and the gate gave up first. `ChatHealthProbe` keeps the last answer instead: its own thread
(ordo/control/serve.py) refreshes it every few seconds, so a slow probe delays nobody, and a reader
never touches the network.
"""
from __future__ import annotations

import threading
import time
import urllib.request
from collections.abc import Callable

PROBE_INTERVAL_SECONDS = 3.0
PROBE_TIMEOUT_SECONDS = 2.0
# An answer older than this many intervals is unknown, not "still up": the probe thread died.
STALE_AFTER_INTERVALS = 4


def _fetch_status(url: str, timeout: float) -> bool:
    """True when `url` answers 200 within `timeout`; any failure (down, refusing, loading 503,
    unresolvable) is "not up"."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - every failure to answer 200 means the engine is not up
        return False


class ChatHealthProbe:
    def __init__(self, url: str | None, interval: float = PROBE_INTERVAL_SECONDS,
                 timeout: float = PROBE_TIMEOUT_SECONDS,
                 fetch: Callable[[str, float], bool] = _fetch_status,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.interval = interval
        self.timeout = timeout
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._answer: tuple[float, bool] | None = None  # (when probed, up)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def refresh(self) -> None:
        """One probe, recorded. Called by the probe thread; a no-op when unconfigured."""
        if not self.url:
            return
        up = self._fetch(self.url, self.timeout)
        with self._lock:
            self._answer = (self._clock(), up)

    def up(self) -> bool | None:
        """The last probe's answer. None when unconfigured, not probed yet, or stale."""
        with self._lock:
            answer = self._answer
        if answer is None:
            return None
        probed_at, up = answer
        if self._clock() - probed_at > self.interval * STALE_AFTER_INTERVALS:
            return None
        return up

    def run_forever(self) -> None:
        """The probe thread's body."""
        while True:
            self.refresh()
            time.sleep(self.interval)
