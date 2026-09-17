"""Trajectory metrics and the private-dataset generator, against a tiny synthetic state.db built to
the same schema as Hermes v0.20.0 (sessions + messages)."""
from __future__ import annotations

import json
import sqlite3

import pytest
from ordo_evals.private_dataset import (
    build_private_dataset,
    candidate_asks,
    clean_message,
    is_self_contained_question,
    is_tool_directed,
)
from ordo_evals.trajectory import session_metrics, tool_result_is_error

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, parent_session_id TEXT,
                       started_at REAL NOT NULL, ended_at REAL, input_tokens INTEGER DEFAULT 0,
                       output_tokens INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
                       content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL);
"""


def tool_call(name, **arguments):
    return {"id": f"c-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-1', 'api_server', 'local-chat', 1000.0, 1200, 340)")
    # a compaction child: its rows belong to the same item
    connection.execute("INSERT INTO sessions (id, source, model, parent_session_id, started_at, input_tokens, "
                       "output_tokens) VALUES ('eval-1-c', 'api_server', 'local-chat', 'eval-1', 1010.0, 90, 10)")
    rows = [
        ("eval-1", "user", "do the thing", None, 1000.0),
        ("eval-1", "assistant", "", json.dumps([tool_call("terminal", command="ls")]), 1001.0),
        ("eval-1", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 1002.0),
        ("eval-1", "assistant", "", json.dumps([tool_call("terminal", command="ls")]), 1003.0),  # repeat
        ("eval-1", "tool", json.dumps({"exit_code": 2, "output": "", "error": "boom"}), None, 1004.0),
        ("eval-1-c", "assistant", "RESULT: done", None, 1012.0),
    ]
    connection.executemany("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
                           "VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path


def test_session_metrics_cover_the_compaction_child(state_db):
    metrics = session_metrics(state_db, "eval-1")
    assert metrics["found"] is True
    assert metrics["session_ids"] == ["eval-1", "eval-1-c"]
    assert metrics["model"] == "local-chat"
    assert metrics["turns"] == 3
    assert metrics["tool_calls"] == 2
    assert metrics["tool_names"] == ["terminal"]
    assert metrics["repeated_calls"] == 1
    assert metrics["tool_errors"] == 1
    assert metrics["prompt_tokens"] == 1290 and metrics["completion_tokens"] == 350
    assert metrics["db_span_s"] == 12.0


def test_missing_session_is_reported_not_invented(state_db):
    assert session_metrics(state_db, "no-such-session") == {"found": False, "session_id": "no-such-session"}


@pytest.mark.parametrize(("content", "is_error"), [
    (json.dumps({"exit_code": 0, "output": "fine"}), False),
    (json.dumps({"exit_code": 1, "output": ""}), True),
    (json.dumps({"error": "not found"}), True),
    (json.dumps({"success": False}), True),
    (json.dumps({"success": True}), False),
    ("Error: no such file", True),
    ("plain output", False),
    (None, False),
])
def test_tool_result_is_error(content, is_error):
    assert tool_result_is_error(content) is is_error


# ── private dataset generator ──────────────────────────────────────────────────

@pytest.fixture
def discord_db(tmp_path):
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, started_at) VALUES ('d1', 'discord', 1.0)")
    connection.execute("INSERT INTO sessions (id, source, started_at) VALUES ('c1', 'cron', 1.0)")
    keep = [
        "[opname] How do I rotate the LiteLLM virtual keys for a new consumer?",
        "[opname] What is the difference between a compose profile and a plugin here?",
        "Explain how the GPU lease works when two renders are queued.",
        "[opname] how do i rotate the litellm virtual keys for a NEW consumer?",  # duplicate of the first
    ]
    drop = [
        "[opname] yes, do that",                                    # anaphora
        "[opname] try again with the same thing",                   # refers to earlier turns
        "[CONTEXT COMPACTION] summary of the previous conversation", # injected annotation
        "[opname] ok",                                              # too short
        "[opname] What about <@123456789012345678> and his branch?", # mention
        "[opname] Is https://example.com/docs correct?",             # link
        "[opname] " + "x" * 600,                                    # too long
    ]
    connection.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 1.0)",
                           [("d1", text) for text in keep + drop])
    connection.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                       "VALUES ('d1', 'assistant', 'What should I do next?', 1.0)")
    connection.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                       "VALUES ('c1', 'user', 'What is the nightly reel status?', 1.0)")
    connection.commit()
    connection.close()
    return path


def test_clean_message_strips_the_speaker_tag_and_drops_annotations():
    assert clean_message("[opname] What is this?") == "What is this?"
    assert clean_message("[CONTEXT COMPACTION] anything") is None
    assert clean_message("   ") is None


@pytest.mark.parametrize(("text", "keep"), [
    ("How do I rotate a key?", True),
    ("Explain the GPU lease rules.", True),
    ("that one again", False),
    ("Is the above still true?", False),
    ("Hi", False),
    ("Check <@123456789012345678> please, what changed?", False),
    # E2: tool/service-directed asks a bare, tool-less model cannot fairly answer (synthetic strings)
    ("Can you search qdrant for the onboarding doc?", False),
    ("What's in my vault about the Q3 roadmap?", False),
    ("Please check the n8n workflow status for me.", False),
    ("Can you check our collection of meeting notes?", False),
    ("Is the docker container for the app healthy?", False),
    ("What does this cron job actually run?", False),
    # E2: follow-ups that depend on prior turns (synthetic strings, not real operator asks)
    ("What about the other roadmap document?", False),
    ("And how long would that normally take?", False),
    ("Also, does it support video uploads?", False),
    ("Is the same one still broken today?", False),
    # E2: below the minimum word count even though it clears the character minimum
    ("Fix the printer?", False),
])
def test_is_self_contained_question(text, keep):
    assert is_self_contained_question(text) is keep


@pytest.mark.parametrize(("text", "tool_directed"), [
    ("Can you search qdrant for the onboarding doc?", True),
    ("What's in my vault about the Q3 roadmap?", True),
    ("Please check the n8n workflow status for me.", True),
    ("Can you check our collection of meeting notes?", True),
    ("Is the docker container for the app healthy?", True),
    ("What does this cron job actually run?", True),
    ("How do I rotate a key?", False),
    ("Explain the GPU lease rules.", False),
    ("What is the capital of France?", False),
])
def test_is_tool_directed(text, tool_directed):
    assert is_tool_directed(text) is tool_directed


def test_candidates_are_discord_user_asks_only_and_deduplicated(discord_db):
    asks = candidate_asks(discord_db)
    assert len(asks) == 3
    assert all("CONTEXT COMPACTION" not in a for a in asks)
    assert not any(a.startswith("[") for a in asks)
    assert "What is the nightly reel status?" not in asks   # cron session, not discord
    assert "What should I do next?" not in asks             # assistant message


def test_build_private_dataset_is_seeded_and_bounded(discord_db):
    items, stats = build_private_dataset(discord_db, n=2, seed=7)
    again, _ = build_private_dataset(discord_db, n=2, seed=7)
    other, _ = build_private_dataset(discord_db, n=3, seed=7)
    assert [i["id"] for i in items] == [i["id"] for i in again]
    assert len(items) == 2 and stats == {"candidates": 3, "selected": 2, "requested": 2, "seed": 7}
    assert len(other) == 3
    assert all(i["source"] == "hermes-state:discord" and i["id"].startswith("pd-") for i in items)
    with pytest.raises(ValueError):
        build_private_dataset(discord_db, n=0, seed=7)
