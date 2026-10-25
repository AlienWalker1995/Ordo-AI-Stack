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
and the manual-stop mark from the stop phase keeps `unless-stopped` from reviving it. Observed live
2026-10-25: a reclaim restart left ComfyUI down until a human noticed. So after a FAILED restart
the sweep re-reads the real state, and if ComfyUI is stopped it starts it back up (under the same
lock) instead of leaving it down.
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
    """One sweep per call (ordo/control/serve.py runs it every SWEEP_SECONDS). `restart` and
    `start` are the lifecycle verbs for ComfyUI, which also refuse while a lease conflicts;
    `start` is the recovery for a failed restart that left the container stopped."""

    def __init__(self, broker: Any, restart: Callable[[], dict[str, Any]],
                 start: Callable[[], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.broker = broker
        self.restart = restart
        self.start = start
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

    def _running(self) -> bool | None:
        """Whether ComfyUI's container is running right now, or None when docker cannot say."""
        try:
            stats = (self.broker.backend.service_stats().get("services") or {}).get(SERVICE) or {}
            return bool(stats.get("running"))
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
            recovery = None
            if not result.get("ok") and self._running() is False:
                # The kill half of the restart landed but the start half never ran (see the
                # module note); unless-stopped will not revive it. Start it back up.
                recovery = self.start() if self.start else None
        finally:
            self.broker.operation_lock.release()
        self.last_busy = now
        if result.get("ok"):
            self.reclaims += 1
            return f"restarted ComfyUI to release the {memory:.1f} GiB it held while idle"
        failure = result.get("error", result)
        if recovery is not None:
            if recovery.get("ok"):
                self.reclaims += 1
                return (f"ComfyUI held {memory:.1f} GiB while idle; its restart failed ({failure}) "
                        f"and left it stopped, so it was started back up")
            return (f"ComfyUI held {memory:.1f} GiB while idle; its restart failed ({failure}) "
                    f"and left it stopped, and the recovery start failed too: "
                    f"{recovery.get('error', recovery)}")
        return f"ComfyUI held {memory:.1f} GiB while idle, but its restart failed: {failure}"
