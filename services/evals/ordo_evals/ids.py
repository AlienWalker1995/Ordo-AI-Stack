"""Deterministic identifiers, so re-running an ingest updates the same Langfuse objects instead of
creating duplicates, and a trace id is known (and recorded in items.jsonl) even with --no-langfuse."""
from __future__ import annotations

import hashlib
import re


def trace_id_for(run_id: str, suite: str, item_id: str) -> str:
    """The Langfuse trace id for one item of one run.

    Identical to langfuse.Langfuse.create_trace_id(seed=...) in the pinned SDK (4.15.1):
    sha256(seed)[:16 bytes] as 32 lowercase hex characters, which is also a valid W3C trace id.
    tests/evals/test_ids.py locks the formula.
    """
    seed = f"{run_id}:{suite}:{item_id}"
    return hashlib.sha256(seed.encode("utf-8")).digest()[:16].hex()


def score_id_for(run_id: str, suite: str, item_id: str, name: str) -> str:
    """A stable Langfuse score id: re-posting the same (run, item, score name) upserts."""
    return hashlib.sha256(f"score:{run_id}:{suite}:{item_id}:{name}".encode()).hexdigest()[:32]


_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_token(value: str, max_len: int = 64) -> str:
    """Reduce `value` to [A-Za-z0-9_-] (a Hermes session id, a vault path segment)."""
    cleaned = _SAFE.sub("-", value).strip("-")
    return cleaned[:max_len] or "x"


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_run_id(run_id: str) -> str:
    """A run id becomes a directory name, a Langfuse run name and part of vault paths: keep it tame."""
    if not RUN_ID_PATTERN.match(run_id or ""):
        raise ValueError(f"run id {run_id!r} must match {RUN_ID_PATTERN.pattern}")
    return run_id
