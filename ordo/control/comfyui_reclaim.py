"""Restart ComfyUI when it sits idle holding a lot of system RAM.

ComfyUI keeps the models it loaded in system RAM after a render. On a Docker Desktop host every
container shares one VM's memory, so an idle ComfyUI holding tens of GiB starves the others (on
the operator's host, the media server's transcodes). A restart is the release that always works;
the next render loads what it needs again.

This used to be a Hermes cron that polled the control plane every 30 minutes and restarted ComfyUI
through it, with a window between its checks and its restart in which a lease could be admitted.
Here the final checks and the restart run under the broker's operation lock, the lock every lease
transition takes, so no lease starts in between. It restarts only when all of these hold, and a
signal it cannot read counts as busy:
  - the GPU is not leased (no lease running or queued, no resident evicted), and has not been for
    QUIET_SECONDS: a render batch's next lease often follows the previous one within seconds;
  - ComfyUI is running and holds at least RECLAIM_MIN_GB (docker stats);
  - ComfyUI's own queue is empty (read inside its container).
A stack without ComfyUI never passes the memory check, so the sweep does nothing there.

`docker restart` is not atomic. On a daemon race ("tried to kill container, but did not receive an
exit event") the stop half lands -- the container is SIGKILLed -- and the start half never runs,
and the manual-stop mark from the stop phase keeps `unless-stopped` from reviving it (observed
live 2026-10-25: a reclaim restart left ComfyUI down until a human noticed). That race is handled
at the seam: the broker's restart waits for the exit to actually land, retries a failed start once
after a short backoff (a transient failure otherwise leaves the container stopped with nothing to
revive it), and confirms the container actually came up before reporting success
(ordo/control/broker.py). A failed restart now carries that precise verdict, so the sweep only
reports it -- it never guesses at the container's state.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

SERVICE = "comfyui"
RECLAIM_MIN_GB = 12.0
QUIET_SECONDS = 600
SWEEP_SECONDS = 300
QUEUE_PROBE = ["python3", "-c",
               "import json, urllib.request; "
               "q = json.load(urllib.request.urlopen('http://127.0.0.1:8188/queue', timeout=10)); "
               "print(len(q['queue_running']) + len(q['queue_pending']))"]


class ComfyUIReclaim:
    """One sweep per call (ordo/control/serve.py runs it every SWEEP_SECONDS). `restart` is the
    lifecycle verb's restart of ComfyUI, which also refuses while a lease conflicts."""

    def __init__(self, broker: Any, restart: Callable[[], dict[str, Any]],
                 *, clock: Callable[[], float] = time.monotonic):
        self.broker = broker
        self.restart = restart
        self.clock = clock
        # A fresh process has not watched the GPU yet, so it waits one quiet period first.
        self.last_busy = clock()
        self.reclaims = 0

    def _leased(self) -> bool:
        return bool(self.broker.scheduler.status()["leased"])

    def _memory_gb(self) -> float | None:
        """ComfyUI's memory in GiB, or None when it is not running or docker cannot say."""
        try:
            stats = (self.broker.backend.service_stats().get("services") or {}).get(SERVICE) or {}
            return float(stats["mem_gb"]) if stats.get("running") else None
        except Exception:  # noqa: BLE001 - unreadable counts as busy
            return None

    def _queue_length(self) -> int | None:
        """Prompts running or pending in ComfyUI, or None when its queue cannot be read."""
        try:
            code, out = self.broker.backend.exec_in_service(SERVICE, QUEUE_PROBE)
            return int(out.strip().splitlines()[-1]) if code == 0 else None
        except Exception:  # noqa: BLE001 - unreadable counts as busy
            return None

    def sweep(self) -> str | None:
        """Check once. Returns a line to log when it restarted ComfyUI or tried to, else None."""
        now = self.clock()
        if self._leased():
            self.last_busy = now
            return None
        if now - self.last_busy < QUIET_SECONDS:
            return None
        # docker stats takes a few seconds, so it is read before taking the lock.
        memory = self._memory_gb()
        if memory is None or memory < RECLAIM_MIN_GB:
            return None
        # A lease transition or a lifecycle verb is running: busy, try on the next sweep.
        if not self.broker.operation_lock.acquire(blocking=False):
            return None
        try:
            if self._leased() or self._queue_length() != 0:
                self.last_busy = now
                return None
            result = self.restart()
        finally:
            self.broker.operation_lock.release()
        self.last_busy = now
        if result.get("ok"):
            self.reclaims += 1
            return f"restarted ComfyUI to release the {memory:.1f} GiB it held while idle"
        return f"ComfyUI held {memory:.1f} GiB while idle, but its restart failed: {result.get('error', result)}"
