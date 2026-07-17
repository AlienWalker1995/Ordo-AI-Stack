"""GPU scheduler — the decision engine that replaced the reactive guardian.

The old reactive guardian let ComfyUI evict llama.cpp and raced to recover (VRAM leaks,
deadlocks). Instead, all GPU work requests capacity here. Policy (from the architecture
decisions):

  - FIFO admission — no priority, no preemption of a running job.
  - Co-run when it fits — the queue head is admitted concurrently with running jobs whenever
    it fits in free VRAM (declared footprint), so a small chat job slips beside a media render
    instead of waiting. This kills the starvation cliff.
  - Per-item batches — a batch is many small entries, not one blocking monolith, so other work
    (chat) can interleave between items.
  - No eviction of RUNNING jobs (they run to completion); idle *cached* models are LRU-evictable
    to reclaim VRAM (modeled via `unload_idle`).
  - RESTORE evicted residents on completion — a media job that evicted the resident LLM (to free
    VRAM) gets that resident RESTARTED once the GPU-heavy work drains. This is the "media lease"
    contract: a lease STOPS the resident for the job's duration and the scheduler RESTORES it when
    the lease ends. Restoration is deferred until the queue drains below the resident's footprint
    (no thrash between back-to-back renders), and a per-job lease TTL auto-completes a stranded job
    so a crashed client can NEVER permanently kill the resident (V1's fatal flaw).

This module is the pure decision logic; a thin process broker (start/stop containers) drives it
against real processes (see broker.py). The broker does all container start/stop; the scheduler
only ever DECIDES which residents to evict and which to restore.
"""
from __future__ import annotations

import dataclasses
import itertools

# A lease with an unknown/zero estimate still gets a hard cap so a stranded job (crashed client
# that never POSTs /jobs/complete) can't hold the resident down forever. The TTL for a job with a
# known estimate is est_seconds * LEASE_TTL_EST_MULT; both are clamped to LEASE_TTL_MAX_SECONDS.
DEFAULT_LEASE_TTL_SECONDS = 1800.0  # 30 min — cap for a job with no estimate
LEASE_TTL_EST_MULT = 2.0           # a job may legitimately run up to ~2x its estimate
LEASE_TTL_MAX_SECONDS = 3600.0     # absolute ceiling — no lease outlives this

# Renewable leases: a client that HEARTBEATS proves liveness, so its lease is extended past the
# estimate-based caps above (those defend against clients that never call /jobs/complete — a
# heartbeating client that dies simply stops beating and is swept within HEARTBEAT_TTL_SECONDS).
# This is what lets a multi-hour job (LoRA training) hold a lease without weakening self-heal.
HEARTBEAT_TTL_SECONDS = 900.0


@dataclasses.dataclass
class Job:
    id: str
    vram_gb: float
    kind: str = "generic"  # chat | media | batch_item
    est_seconds: float = 0.0  # estimated duration (0 = unknown); drives the busy-ETA + lease TTL


class Scheduler:
    def __init__(
        self,
        total_vram_gb: float,
        cloud_fallback: bool = False,
        lease_ttl_default: float = DEFAULT_LEASE_TTL_SECONDS,
        lease_ttl_max: float = LEASE_TTL_MAX_SECONDS,
        heartbeat_ttl: float = HEARTBEAT_TTL_SECONDS,
    ):
        self.total_vram_gb = float(total_vram_gb)
        self.cloud_fallback = bool(cloud_fallback)
        self.lease_ttl_default = float(lease_ttl_default)
        self.lease_ttl_max = float(lease_ttl_max)
        self.heartbeat_ttl = float(heartbeat_ttl)
        self._queue: list[Job] = []
        self._running: dict[str, Job] = {}
        self._elapsed: dict[str, float] = {}      # running job id -> seconds elapsed
        self._deadline: dict[str, float] = {}     # running job id -> clock time its lease expires
        self._started: dict[str, float] = {}      # running job id -> clock time it was admitted
        self._clock: float = 0.0                  # monotonic-ish clock advanced by tick()
        self._idle_cached: dict[str, float] = {}  # id -> vram held by an idle/cached model
        self._evicted: dict[str, float] = {}      # resident id -> vram it held (stopped, awaiting restore)
        self._lru = itertools.count()             # recency counter for cached models
        self._lru_order: dict[str, int] = {}
        self._cloud_routed: list[str] = []        # too-big jobs sent to cloud (fallback enabled)
        self._rejected: list[str] = []            # too-big jobs with no fallback (can't run)

    # --- introspection ---
    @property
    def running_ids(self) -> list[str]:
        return list(self._running)

    @property
    def queued_ids(self) -> list[str]:
        return [j.id for j in self._queue]

    @property
    def idle_cached(self) -> dict[str, float]:
        return dict(self._idle_cached)

    @property
    def evicted_residents(self) -> dict[str, float]:
        """Residents the scheduler has STOPPED to free VRAM and will restore when work drains."""
        return dict(self._evicted)

    @property
    def used_vram_gb(self) -> float:
        # Evicted residents hold NO VRAM (they're stopped) — only running jobs + still-cached idles.
        return sum(j.vram_gb for j in self._running.values()) + sum(self._idle_cached.values())

    @property
    def free_vram_gb(self) -> float:
        return self.total_vram_gb - self.used_vram_gb

    # --- lifecycle ---
    def submit(self, job: Job) -> None:
        self._queue.append(job)

    def cache_idle(self, model_id: str, vram_gb: float) -> None:
        """A model loaded but not actively serving — reclaimable via LRU eviction.

        Registering the resident LLM here at startup is what lets a media job evict it: without a
        cached entry the scheduler thinks the whole card is free and never frees the LLM's VRAM.
        Re-calling this after a restore re-arms the resident as evictable for the next lease.
        """
        self._idle_cached[model_id] = vram_gb
        self._lru_order[model_id] = next(self._lru)
        # a re-cached resident is no longer "evicted/pending restore"
        self._evicted.pop(model_id, None)

    def _lease_ttl(self, job: Job) -> float:
        """The hard cap after which a lease is force-completed (self-healing against a stranded job)."""
        base = job.est_seconds * LEASE_TTL_EST_MULT if job.est_seconds > 0 else self.lease_ttl_default
        return min(base, self.lease_ttl_max)

    def _unload_lru_until(self, need_gb: float) -> list[str]:
        """Evict idle cached models (LRU first) until `need_gb` would fit. Returns evicted ids.

        Evicted residents move to `_evicted` (tracked with their footprint) so the broker can
        RESTART them once the GPU-heavy work drains — the missing half of the media-lease contract.
        """
        evicted = []
        while self.free_vram_gb < need_gb and self._idle_cached:
            victim = min(self._idle_cached, key=lambda m: self._lru_order.get(m, 0))
            held = self._idle_cached.pop(victim, 0.0)
            self._lru_order.pop(victim, None)
            self._evicted[victim] = held
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
                # Can NEVER fit on this GPU. Removing it (route to cloud, or reject) instead of
                # blocking is the fix for a real starvation bug: a too-big head would otherwise
                # stall every smaller job queued behind it forever. Then keep pumping the rest.
                self._queue.pop(0)
                (self._cloud_routed if self.cloud_fallback else self._rejected).append(head.id)
                continue
            if head.vram_gb > self.free_vram_gb:
                # try reclaiming idle cached VRAM before giving up (LRU evict, not preemption)
                evicted += self._unload_lru_until(head.vram_gb)
            if head.vram_gb <= self.free_vram_gb:
                job = self._queue.pop(0)
                self._running[job.id] = job
                self._elapsed[job.id] = 0.0
                self._deadline[job.id] = self._clock + self._lease_ttl(job)
                self._started[job.id] = self._clock
                admitted.append(job.id)
            else:
                break  # strict FIFO — head waits for a running job to complete
        return admitted, evicted

    def _restorable_residents(self) -> list[str]:
        """Which evicted residents can be restored NOW without immediately re-evicting them.

        No-thrash rule: only restore a resident once its footprint fits in the CURRENTLY-free VRAM
        AND no queued job is waiting on VRAM. This defers restoration until the media queue drains
        below the resident's footprint, so back-to-back renders don't flap the resident on and off.
        Restores largest-first (the resident LLM is the big one) and stops as soon as the next one
        wouldn't fit, re-checking against a running free-VRAM tally.
        """
        if any(j.vram_gb > self.free_vram_gb for j in self._queue):
            # a job is still waiting on VRAM — do not restore into contention
            return []
        if self._queue:
            # queued work exists that DOES fit (will be admitted next pump) — let it run first;
            # restoring now would just get re-evicted. Wait until the queue is empty.
            return []
        free = self.free_vram_gb
        restore: list[str] = []
        for rid in sorted(self._evicted, key=lambda r: self._evicted[r], reverse=True):
            if self._evicted[rid] <= free:
                restore.append(rid)
                free -= self._evicted[rid]
        return restore

    def take_restorable(self) -> dict[str, float]:
        """Pop + return the residents that should be restarted now (id -> vram). Broker starts them.

        The scheduler immediately re-accounts their VRAM as idle-cached (they'll be resident again),
        so a follow-up pump sees the card as it will actually be. Idempotent when nothing is due.
        """
        due = self._restorable_residents()
        out: dict[str, float] = {}
        for rid in due:
            held = self._evicted.pop(rid, 0.0)
            out[rid] = held
            self.cache_idle(rid, held)
        return out

    def complete(self, job_id: str) -> None:
        self._running.pop(job_id, None)
        self._elapsed.pop(job_id, None)
        self._deadline.pop(job_id, None)
        self._started.pop(job_id, None)

    def heartbeat(self, job_id: str) -> bool:
        """Renew a running job's lease: deadline moves to now + heartbeat_ttl (liveness-based).

        Deliberately NOT capped by lease_ttl_max — the cap defends against clients that never
        complete, and a heartbeating client re-proves liveness on every beat. Returns False (and
        changes nothing) for a job that isn't running, so a client can detect a lost lease (e.g.
        across an ops-controller restart) and re-acquire.
        """
        if job_id not in self._running:
            return False
        self._deadline[job_id] = self._clock + self.heartbeat_ttl
        return True

    def sweep_expired_leases(self) -> list[str]:
        """Force-complete any running job whose lease TTL has elapsed. Returns the swept ids.

        This is the self-heal: a crashed client that never calls /jobs/complete would otherwise
        strand its lease and keep the resident evicted forever (V1's fatal deadlock). After the TTL
        the scheduler completes the job itself; the broker then restores the resident on the next
        reconcile. Driven by the clock that tick() advances.
        """
        expired = [jid for jid, dl in self._deadline.items() if self._clock >= dl]
        for jid in expired:
            self.complete(jid)
        return expired

    def drain_cloud_routed(self) -> list[str]:
        """Return + clear the jobs routed to cloud, so the agent dispatches each exactly once."""
        routed, self._cloud_routed = self._cloud_routed, []
        return routed

    def tick(self, dt_seconds: float) -> None:
        """Advance elapsed time for running jobs (drives the ETA) and the lease clock."""
        self._clock += dt_seconds
        for jid in self._running:
            self._elapsed[jid] = self._elapsed.get(jid, 0.0) + dt_seconds

    def _remaining(self, jid: str) -> float:
        est = self._running[jid].est_seconds
        return max(0.0, est - self._elapsed.get(jid, 0.0)) if est else 0.0

    def _lease_remaining(self, jid: str) -> float:
        return max(0.0, self._deadline.get(jid, self._clock) - self._clock)

    def status(self) -> dict:
        """The status contract polled by the dashboard/agents (the 'GPU busy, ~Ns' source)."""
        head_fits = bool(self._queue) and self._queue[0].vram_gb <= self.free_vram_gb
        waiting = bool(self._queue) and not head_fits
        # ETA to the next free slot: soonest running job to finish (only meaningful while waiting)
        rem = [self._remaining(j) for j in self._running if self._running[j].est_seconds]
        eta = round(min(rem), 1) if (waiting and rem) else (0.0 if head_fits else None)
        return {
            "state": "busy" if self._running else "idle",
            "total_vram_gb": self.total_vram_gb,
            "free_vram_gb": round(self.free_vram_gb, 1),
            "running": [
                {"id": j, "kind": self._running[j].kind,
                 "remaining_s": round(self._remaining(j), 1),
                 "lease_ttl_s": round(self._lease_remaining(j), 1),
                 "held_s": round(self._clock - self._started.get(j, self._clock), 1)}
                for j in self._running
            ],
            "queued": [{"id": j.id, "kind": j.kind, "vram_gb": j.vram_gb} for j in self._queue],
            "waiting_on_vram": waiting,
            "eta_seconds": eta,
            # the media-lease surface: residents currently stopped to free VRAM, awaiting restore
            "idle_cached": {k: round(v, 1) for k, v in self._idle_cached.items()},
            "evicted_residents": {k: round(v, 1) for k, v in self._evicted.items()},
            "cloud_routed": list(self._cloud_routed),
            "rejected": list(self._rejected),
        }
