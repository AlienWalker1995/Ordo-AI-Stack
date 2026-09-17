"""The results history: one JSON object per (run, suite, metric) in /results/history.jsonl.

This file is the future leaderboard's input, so its row shape is a contract:

    {"run_id": str, "ts": ISO-8601 UTC str, "suite": str, "subject": "model" | "harness",
     "model": str, "harness": str | null, "metric": str, "value": float, "n": int,
     "ci95": [low, high] | null, "commit": str | null, "dirty": bool | null}

  * subject says WHAT was measured. A model row measures the bare model (harness is null); a harness
    row measures Hermes end to end, and `model` records which model Hermes ran on, so a later run of
    the same harness on a different model is comparable row for row.
  * ci95 is the 95% interval of `value` (see stats.py); null when it cannot be estimated.
  * commit/dirty (E7) are the git provenance of the mounted `services/evals` code that produced this
    row: `commit` is the 40-hex-char HEAD sha the run started from, or null when it could not be
    determined (the run was not launched via `scripts/evals/run.sh`, see runner._provenance_gate).
    `dirty` is true when that checkout had uncommitted changes under services/evals at run start,
    false when it was clean, null when unknown. A run only reaches here with dirty=true or
    commit=null at all if it was started with `--allow-dirty` - see runner.run().

Appends are the normal path. `replace_run_suites` exists for ingest-grades, which recomputes the
rows of a run it has already written (judge metrics arrive after the run).
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ordo_evals.jsonl import append_jsonl, read_jsonl, write_jsonl

HISTORY_FIELDS = ("run_id", "ts", "suite", "subject", "model", "harness", "metric", "value", "n", "ci95",
                  "commit", "dirty")
SUBJECTS = ("model", "harness")


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_row(*, run_id: str, ts: str, suite: str, subject: str, model: str, harness: str | None,
             metric: str, value: float, n: int, ci95: list[float] | None,
             commit: str | None = None, dirty: bool | None = None) -> dict[str, Any]:
    row = {"run_id": run_id, "ts": ts, "suite": suite, "subject": subject, "model": model,
           "harness": harness, "metric": metric, "value": value, "n": n, "ci95": ci95,
           "commit": commit, "dirty": dirty}
    validate_row(row)
    return row


def validate_row(row: dict[str, Any]) -> None:
    """Raise ValueError if `row` is not a valid history row. Exact key set: no extras, none missing."""
    keys = set(row)
    expected = set(HISTORY_FIELDS)
    if keys != expected:
        raise ValueError(f"history row keys {sorted(keys)} != {sorted(expected)}")
    for field in ("run_id", "ts", "suite", "metric", "model"):
        if not isinstance(row[field], str) or not row[field]:
            raise ValueError(f"history row field {field!r} must be a non-empty string")
    if row["subject"] not in SUBJECTS:
        raise ValueError(f"history row subject must be one of {SUBJECTS}, got {row['subject']!r}")
    if row["subject"] == "model" and row["harness"] is not None:
        raise ValueError("a model-subject row must have harness null (the model was called directly)")
    if row["subject"] == "harness" and not (isinstance(row["harness"], str) and row["harness"]):
        raise ValueError("a harness-subject row must name the harness")
    try:
        _dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"history row ts {row['ts']!r} is not ISO-8601") from exc
    if isinstance(row["value"], bool) or not isinstance(row["value"], int | float):
        raise ValueError("history row value must be a number")
    if isinstance(row["n"], bool) or not isinstance(row["n"], int) or row["n"] < 0:
        raise ValueError("history row n must be a non-negative integer")
    ci = row["ci95"]
    if ci is not None:
        if (not isinstance(ci, list) or len(ci) != 2
                or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in ci) or ci[0] > ci[1]):
            raise ValueError("history row ci95 must be null or [low, high] with low <= high")
    if row["commit"] is not None and not (isinstance(row["commit"], str) and row["commit"]):
        raise ValueError("history row commit must be null or a non-empty string")
    if row["dirty"] is not None and not isinstance(row["dirty"], bool):
        raise ValueError("history row dirty must be null or a bool")


def append_rows(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    for row in rows:
        validate_row(row)
    append_jsonl(path, rows)


def replace_run_suites(path: str | Path, run_id: str, suites: Iterable[str],
                       rows: Iterable[dict[str, Any]]) -> None:
    """Drop every existing row of `run_id` for `suites`, then append `rows` (validated first)."""
    rows = list(rows)
    for row in rows:
        validate_row(row)
    suites = set(suites)
    existing = read_jsonl(path) if Path(path).exists() else []
    kept = [r for r in existing if not (r.get("run_id") == run_id and r.get("suite") in suites)]
    write_jsonl(path, kept + rows)
