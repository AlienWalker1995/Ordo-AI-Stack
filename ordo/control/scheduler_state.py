"""Scheduler state on disk: the lease and eviction state that must survive an ops-controller restart.

The scheduler keeps its state in memory (running leases, the queue, evicted residents, lease
deadlines). Losing it mid-lease is dangerous: the evicted resident (llama.cpp) is never restored,
or is restored beside a render that still holds VRAM (two tenants on one card crashed the host on
2026-08-08). So the broker writes the state here on every transition, and the next process adopts
it before serving (`Broker.restore_state`).

The scheduler's clock is relative (advanced by the serve loop's tick) and starts at zero in every
process, so deadlines are written as wall-clock instants and converted back on load: a lease that
expired while the controller was down arrives with a deadline in the past and is swept.

The write is atomic (a temp file in the same directory, fsynced, then renamed over the old one), so
a crash mid-write leaves the previous state, never a torn file. A file that is still unreadable (a
different version, bad JSON, a missing field) raises StateUnreadable, and the caller starts
conservatively instead of guessing.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

STATE_VERSION = 1

# The synthetic lease that holds a stopped resident evicted when the saved state is unreadable.
RECOVERY_JOB_ID = "recovery-unknown-lease-state"


class StateUnreadable(Exception):
    """The state file exists but cannot be trusted. The caller must start conservatively."""


class SchedulerStateStore:
    def __init__(self, path: str | Path, now_fn: Callable[[], float] = time.time):
        self.path = Path(path)
        self.now_fn = now_fn

    def save(self, snapshot: dict) -> None:
        """Write a `Scheduler.snapshot()` atomically. Raises OSError when the write fails."""
        now = self.now_fn()
        doc = {
            "version": STATE_VERSION,
            "saved_at": now,
            "running": [
                {"id": job["id"], "vram_gb": job["vram_gb"], "kind": job["kind"],
                 "est_seconds": job["est_seconds"],
                 "lease_deadline": now + job["lease_remaining_s"],
                 "started_at": now - job["held_s"]}
                for job in snapshot["running"]
            ],
            "queued": snapshot["queued"],
            "evicted": snapshot["evicted"],
            "rejected": snapshot["rejected"],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)

    def load(self) -> dict | None:
        """The saved state as a `Scheduler.load_snapshot()` argument, or None when no file exists.

        Raises StateUnreadable when the file exists but cannot be read, parsed or trusted.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StateUnreadable(f"cannot read {self.path}: {e}") from e
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise StateUnreadable(f"{self.path} is not valid JSON: {e}") from e
        if not isinstance(doc, dict):
            raise StateUnreadable(f"{self.path} does not hold a JSON object")
        if doc.get("version") != STATE_VERSION:
            raise StateUnreadable(f"{self.path} has version {doc.get('version')!r}; "
                                  f"this ops-controller reads version {STATE_VERSION}")
        now = self.now_fn()
        try:
            return {
                "running": [
                    {"id": str(job["id"]), "vram_gb": float(job["vram_gb"]), "kind": str(job["kind"]),
                     "est_seconds": float(job["est_seconds"]),
                     "lease_remaining_s": float(job["lease_deadline"]) - now,
                     "held_s": now - float(job["started_at"])}
                    for job in doc["running"]
                ],
                "queued": [
                    {"id": str(job["id"]), "vram_gb": float(job["vram_gb"]), "kind": str(job["kind"]),
                     "est_seconds": float(job["est_seconds"])}
                    for job in doc["queued"]
                ],
                "evicted": {str(name): float(vram) for name, vram in doc["evicted"].items()},
                "rejected": [str(job_id) for job_id in doc.get("rejected", [])],
            }
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise StateUnreadable(f"{self.path} is missing or has a malformed field ({e!r})") from e
