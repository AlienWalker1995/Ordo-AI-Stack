"""GPU scheduler — the decision engine that replaces the reactive guardian.

The current stack lets ComfyUI evict llama.cpp and races to recover (VRAM leaks, deadlocks).
Instead, all GPU work requests capacity here. Policy (from the architecture decisions):

  - FIFO admission — no priority, no preemption of a running job.
  - Co-run when it fits — the queue head is admitted concurrently with running jobs whenever
    it fits in free VRAM (declared footprint), so a small chat job slips beside a media render
    instead of waiting. This kills the starvation cliff.
  - Per-item batches — a batch is many small entries, not one blocking monolith, so other work
    (chat) can interleave between items.
  - No eviction of RUNNING jobs (they run to completion); idle *cached* models are LRU-evictable
    to reclaim VRAM (modeled via `unload_idle`).

This module is the pure decision logic; a thin process broker (start/stop containers) drives it
against real processes — that part needs the live stack and is out of scope for this slice.
"""
from __future__ import annotations

import dataclasses
import itertools


@dataclasses.dataclass
class Job:
    id: str
    vram_gb: float
    kind: str = "generic"  # chat | media | batch_item


class Scheduler:
    def __init__(self, total_vram_gb: float):
        self.total_vram_gb = float(total_vram_gb)
        self._queue: list[Job] = []
        self._running: dict[str, Job] = {}
        self._idle_cached: dict[str, float] = {}  # id -> vram held by an idle/cached model
        self._lru = itertools.count()             # recency counter for cached models
        self._lru_order: dict[str, int] = {}

    # --- introspection ---
    @property
    def running_ids(self) -> list[str]:
        return list(self._running)

    @property
    def queued_ids(self) -> list[str]:
        return [j.id for j in self._queue]

    @property
    def used_vram_gb(self) -> float:
        return sum(j.vram_gb for j in self._running.values()) + sum(self._idle_cached.values())

    @property
    def free_vram_gb(self) -> float:
        return self.total_vram_gb - self.used_vram_gb

    # --- lifecycle ---
    def submit(self, job: Job) -> None:
        self._queue.append(job)

    def cache_idle(self, model_id: str, vram_gb: float) -> None:
        """A model loaded but not actively serving — reclaimable via unload_idle()."""
        self._idle_cached[model_id] = vram_gb
        self._lru_order[model_id] = next(self._lru)

    def _unload_lru_until(self, need_gb: float) -> list[str]:
        """Evict idle cached models (LRU first) until `need_gb` would fit. Returns evicted ids."""
        evicted = []
        while self.free_vram_gb < need_gb and self._idle_cached:
            victim = min(self._idle_cached, key=lambda m: self._lru_order.get(m, 0))
            self._idle_cached.pop(victim, None)
            self._lru_order.pop(victim, None)
            evicted.append(victim)
        return evicted

    def pump(self) -> tuple[list[str], list[str]]:
        """Admit queue-head jobs while the head fits (co-run). FIFO: a non-fitting head blocks.

        Returns (admitted_ids, evicted_idle_ids).
        """
        admitted: list[str] = []
        evicted: list[str] = []
        while self._queue:
            head = self._queue[0]
            if head.vram_gb > self.total_vram_gb:
                # can never fit on this GPU — leave it (a real broker would cloud-fallback/error)
                break
            if head.vram_gb > self.free_vram_gb:
                # try reclaiming idle cached VRAM before giving up (LRU evict, not preemption)
                evicted += self._unload_lru_until(head.vram_gb)
            if head.vram_gb <= self.free_vram_gb:
                self._running[head.id] = self._queue.pop(0)
                admitted.append(head.id)
            else:
                break  # strict FIFO — head waits for a running job to complete
        return admitted, evicted

    def complete(self, job_id: str) -> None:
        self._running.pop(job_id, None)
