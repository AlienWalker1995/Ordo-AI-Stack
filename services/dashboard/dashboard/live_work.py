"""Live model work: what each caller has the local engines doing now, and did in the last minutes.

The model gateway's throughput callback POSTs a `started` event when it hands a request to an
engine and an `ended` event when that attempt finishes (POST /api/work/events). The store keeps
an in-flight map by call_id and a bounded ring of ended attempts. It is in memory only and
persists nothing: a dashboard restart starts empty, which is right for a live view.

Pure logic (no I/O): the routes in app.py feed it events and the CPU engine's /slots read.
"""
from __future__ import annotations

import statistics
import threading
from collections import deque

INFLIGHT_TTL_S = 30 * 60  # safety net for a start whose end was lost (gateway restart, dropped POST)
RECENT_MAX = 1000
RECENT_MAX_AGE_S = 24 * 3600
WINDOW_S = 300
# The two chat engines always appear under "engines", even idle.
ENGINES = ("llamacpp", "llamacpp-cpu")

_EVENT_FIELDS = ("call_id", "caller", "model_group", "model", "deployment", "prompt_tokens", "fallback")


class WorkStore:
    """In-flight and recently ended model requests, guarded by one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, dict] = {}
        self._recent: deque[dict] = deque(maxlen=RECENT_MAX)
        # call_id -> ended_at of its last end. The gateway posts both events without waiting, so a
        # start can arrive after its own end; this keeps that late start out of the in-flight map.
        self._ended_at: dict[str, float] = {}

    def started(self, event: dict, now: float) -> None:
        started_at = event.get("started_at") or now
        with self._lock:
            self._prune(now)
            ended_at = self._ended_at.get(event["call_id"])
            if ended_at is not None and ended_at >= started_at:
                return
            record = {key: event.get(key) for key in _EVENT_FIELDS}
            record["started_at"] = started_at
            self._inflight[event["call_id"]] = record

    def ended(self, event: dict, now: float) -> None:
        ended_at = event.get("ended_at") or now
        with self._lock:
            self._prune(now)
            start = self._inflight.pop(event["call_id"], None)
            if start is not None and start["started_at"] > ended_at:
                # A newer attempt for this call_id is already in flight (the fallback hop); this
                # end belongs to the older attempt, so put the newer start back and keep it visible.
                self._inflight[event["call_id"]] = start
            start = start or {}
            record = {key: event.get(key) if event.get(key) is not None else start.get(key) for key in _EVENT_FIELDS}
            record["started_at"] = event.get("started_at") or start.get("started_at") or ended_at
            record["ended_at"] = ended_at
            record["completion_tokens"] = event.get("completion_tokens")
            record["outcome"] = event.get("outcome") or "ok"
            record["error"] = event.get("error")
            self._recent.append(record)
            self._ended_at[event["call_id"]] = ended_at

    def _prune(self, now: float) -> None:
        """Drop expired in-flight starts and ended records past their age. Call holding the lock."""
        for call_id in [c for c, r in self._inflight.items() if now - r["started_at"] > INFLIGHT_TTL_S]:
            del self._inflight[call_id]
        while self._recent and now - self._recent[0]["ended_at"] > RECENT_MAX_AGE_S:
            self._recent.popleft()
        for call_id in [c for c, t in self._ended_at.items() if now - t > INFLIGHT_TTL_S]:
            del self._ended_at[call_id]

    def snapshot(self, now: float) -> dict:
        """The /api/work body without the CPU engine's prefill (the route adds that)."""
        with self._lock:
            self._prune(now)
            inflight = [dict(r) for r in self._inflight.values()]
            recent = [r for r in self._recent if now - r["ended_at"] <= WINDOW_S]
        for record in inflight:
            record["running_s"] = round(max(0.0, now - record["started_at"]), 1)
        inflight.sort(key=lambda r: r["started_at"])
        engines: dict[str, dict] = {name: {"inflight": 0} for name in ENGINES}
        for record in inflight:
            engines.setdefault(record["deployment"] or "unknown", {"inflight": 0})["inflight"] += 1
        return {
            "inflight": inflight,
            "recent": {"window_s": WINDOW_S, "callers": caller_summary(recent)},
            "engines": engines,
            "generated_at": round(now, 3),
        }


def caller_summary(records: list[dict]) -> list[dict]:
    """One row per caller over `records`, busiest first."""
    by_caller: dict[str, list[dict]] = {}
    for record in records:
        by_caller.setdefault(record.get("caller") or "unknown", []).append(record)
    rows = []
    for caller, rows_of_caller in by_caller.items():
        latencies = [max(0.0, r["ended_at"] - r["started_at"]) for r in rows_of_caller]
        rows.append({
            "caller": caller,
            "requests": len(rows_of_caller),
            "errors": sum(1 for r in rows_of_caller if r["outcome"] != "ok"),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows_of_caller),
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows_of_caller),
            "p50_latency_s": round(statistics.median(latencies), 2),
            "last_seen": max(r["ended_at"] for r in rows_of_caller),
        })
    rows.sort(key=lambda row: (-row["requests"], row["caller"]))
    return rows


def slots_progress(slots: object) -> dict | None:
    """The CPU engine's prefill progress from llama.cpp's GET /slots: the slots busy with a task,
    summed. None when the answer is not a slot list."""
    if not isinstance(slots, list):
        return None
    busy = [s for s in slots if isinstance(s, dict) and s.get("is_processing")]
    return {
        "processing": bool(busy),
        "prompt_tokens": sum(int(s.get("n_prompt_tokens") or 0) for s in busy),
        "processed": sum(int(s.get("n_prompt_tokens_processed") or 0) for s in busy),
        "cached": sum(int(s.get("n_prompt_tokens_cache") or 0) for s in busy),
    }
