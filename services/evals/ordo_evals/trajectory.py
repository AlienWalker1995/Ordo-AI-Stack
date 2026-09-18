"""Trajectory metrics for one Hermes API-server session, read from Hermes's state.db.

The runner gives every harness item its own session id (the `X-Hermes-Session-Id` request header),
so the session's rows are exactly that item's trajectory. A long turn can be split by context
compaction into child sessions (`sessions.parent_session_id`); those are followed and counted too.

state.db is opened READ-ONLY (`mode=ro` URI) from the hermes-home volume mounted `:ro`. Schema
facts this relies on (Hermes v0.20.0, verified against the live database):
  sessions(id, parent_session_id, model, started_at, ended_at, input_tokens, output_tokens,
           tool_call_count, api_call_count, ...)
  messages(session_id, role, content, tool_calls, tool_name, timestamp, ...)
  assistant `tool_calls` is a JSON list of {"id", "type", "function": {"name", "arguments"}}; a
  tool message's `content` is usually a JSON object whose `error` / `exit_code` / `success` fields
  carry failure.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _session_family(connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """The session plus every descendant created by compaction, oldest first."""
    rows: list[sqlite3.Row] = []
    frontier = [session_id]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop(0)
        if current in seen:
            continue
        seen.add(current)
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (current,)).fetchone()
        if row is not None:
            rows.append(row)
        children = connection.execute(
            "SELECT id FROM sessions WHERE parent_session_id = ? ORDER BY started_at", (current,)).fetchall()
        frontier.extend(child["id"] for child in children)
    return rows


def tool_result_is_error(content: str | None) -> bool:
    """True when a tool message reports failure (see the module docstring for the shapes)."""
    if not content:
        return False
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        lowered = content.lstrip().lower()
        return lowered.startswith(("error", "traceback", "exception"))
    if not isinstance(payload, dict):
        return False
    if payload.get("error"):
        return True
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    return payload.get("success") is False


def _canonical_call(call: dict[str, Any]) -> tuple[str, str]:
    function = call.get("function", {}) or {}
    arguments = function.get("arguments", "")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    return str(function.get("name", "")), json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def session_metrics(db_path: str | Path, session_id: str) -> dict[str, Any]:
    """Metrics for `session_id` (and its compaction children). `found: False` when absent."""
    connection = connect_readonly(db_path)
    try:
        sessions = _session_family(connection, session_id)
        if not sessions:
            return {"found": False, "session_id": session_id}
        ids = [s["id"] for s in sessions]
        placeholders = ",".join("?" for _ in ids)
        messages = connection.execute(
            f"SELECT role, content, tool_calls, timestamp FROM messages WHERE session_id IN ({placeholders}) "
            "ORDER BY timestamp, id", ids).fetchall()
    finally:
        connection.close()

    tool_names: list[str] = []
    seen_calls: set[tuple[str, str]] = set()
    repeated = 0
    turns = 0
    tool_errors = 0
    last_assistant_message: str | None = None
    for message in messages:
        if message["role"] == "assistant":
            turns += 1
            if message["content"]:
                # E10: the assistant's own text, kept even when the turn also made tool calls, so a
                # timed-out item still has SOMETHING to score/judge - see suites/harness.py's
                # call_hermes, which backfills HermesTurn.text from this when the HTTP response never
                # came back but the session was alive.
                last_assistant_message = message["content"]
            try:
                calls = json.loads(message["tool_calls"]) if message["tool_calls"] else []
            except json.JSONDecodeError:
                calls = []
            for call in calls if isinstance(calls, list) else []:
                key = _canonical_call(call)
                tool_names.append(key[0])
                if key in seen_calls:
                    repeated += 1
                seen_calls.add(key)
        elif message["role"] == "tool" and tool_result_is_error(message["content"]):
            tool_errors += 1

    timestamps = [m["timestamp"] for m in messages if m["timestamp"] is not None]
    started = min(s["started_at"] for s in sessions)
    return {
        "found": True,
        "session_id": session_id,
        "session_ids": ids,
        "model": sessions[0]["model"],
        "turns": turns,
        "tool_calls": len(tool_names),
        "tool_names": sorted(set(tool_names)),
        "tool_errors": tool_errors,
        "repeated_calls": repeated,
        "prompt_tokens": sum(int(s["input_tokens"] or 0) for s in sessions),
        "completion_tokens": sum(int(s["output_tokens"] or 0) for s in sessions),
        "db_span_s": round(max(timestamps) - started, 3) if timestamps else None,
        "last_assistant_message": last_assistant_message,
    }
