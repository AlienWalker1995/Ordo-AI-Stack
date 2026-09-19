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
from ordo_evals.trajectory import BEHAVIOUR_FIELDS, behaviour_unknown, session_metrics, tool_result_is_error

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
    metrics = session_metrics(state_db, "eval-1", run_id="r1")
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
    # E10 (round-4 fix): the most recent assistant message that actually carries text - the two
    # tool-call-only assistant turns in eval-1 have empty content and are skipped.
    assert metrics["last_assistant_message"] == "RESULT: done"


def test_missing_session_is_reported_not_invented(state_db):
    assert session_metrics(state_db, "no-such-session", run_id="r1") == {"found": False, "session_id": "no-such-session"}


def test_last_assistant_message_is_none_when_every_assistant_turn_is_tool_calls_only(tmp_path):
    """E10: a session that timed out mid-tool-loop, before ever producing a text reply, must not be
    mistaken for one that has something to recover - last_assistant_message stays None."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-2', 'api_server', 'local-chat', 2000.0, 500, 50)")
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, ?, ?, ?, ?)",
        [("eval-2", "user", "do the thing", None, 2000.0),
         ("eval-2", "assistant", "", json.dumps([tool_call("terminal", command="ls")]), 2001.0),
         ("eval-2", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 2002.0)])
    connection.commit()
    connection.close()
    metrics = session_metrics(path, "eval-2", run_id="r1")
    assert metrics["found"] is True
    assert metrics["last_assistant_message"] is None


def test_behaviour_fields_pair_each_tool_result_with_the_call_that_made_it(tmp_path):
    """Round 7: `session_metrics` is where the ordered (call -> result) pairing happens, so
    `stopping` (E17) and `replay` (E19) can read behaviour without touching sqlite. Here the second
    of five calls comes back "no such note", and the agent keeps going for three more calls."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at) "
                       "VALUES ('eval-r7-harness_ops-ops-01', 'api_server', 'local-chat', 3000.0)")
    prior_session = "eval-earlier-run-harness_ops-ops-01"
    rows = [("eval-r7-harness_ops-ops-01", "user", "find the note", None, 3000.0)]
    results = [json.dumps({"exit_code": 0, "output": "ok"}),
               json.dumps({"exit_code": 1, "output": "cat: note.md: No such file or directory"}),
               json.dumps({"results": [{"session_id": prior_session}]}),
               json.dumps({"exit_code": 0, "output": "ok"}),
               json.dumps({"exit_code": 0, "output": "ok"})]
    for index, result in enumerate(results):
        rows.append(("eval-r7-harness_ops-ops-01", "assistant", "",
                     json.dumps([tool_call("terminal", step=index)]), 3001.0 + index * 2))
        rows.append(("eval-r7-harness_ops-ops-01", "tool", result, None, 3002.0 + index * 2))
    connection.executemany("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
                           "VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()

    metrics = session_metrics(path, "eval-r7-harness_ops-ops-01", run_id="r7")
    assert metrics["behaviour_known"] is True
    assert metrics["first_negative_index"] == 2                 # E17
    assert metrics["first_negative_kind"] == "does_not_exist"
    assert metrics["calls_after_first_negative"] == 3
    assert metrics["explored_after_negative"] is False          # 3 is within the threshold
    assert metrics["replay_aware"] is True                      # E19
    assert metrics["replay_prior_run_ids"] == ["earlier-run"]


def test_a_session_with_no_negative_and_no_prior_run_reports_so(state_db):
    metrics = session_metrics(state_db, "eval-1", run_id="r1")
    assert metrics["behaviour_known"] is True
    assert metrics["first_negative_index"] is None and metrics["calls_after_first_negative"] is None
    assert metrics["replay_aware"] is False and metrics["replay_prior_run_ids"] == []


def test_behaviour_unknown_is_the_shape_for_a_session_that_cannot_be_read():
    """The backfill command's record for a session state.db no longer holds: nulls plus the flag that
    keeps the item out of every behaviour denominator (summary._behaviour_metrics)."""
    unknown = behaviour_unknown()
    assert unknown["behaviour_known"] is False
    assert set(BEHAVIOUR_FIELDS) == set(unknown)
    assert all(unknown[field] is None for field in BEHAVIOUR_FIELDS if field != "behaviour_known")


# ── E22 (round-10 fix): every reading is bounded to the item's own window ────────

NO_SUCH_FILE = json.dumps({"exit_code": 1, "output": "cat: note.md: No such file or directory"})


@pytest.fixture
def abandoned_db(tmp_path):
    """The recorded shape of an item that exceeded its budget: the harness stopped waiting at 900s,
    Hermes carried on to 1773s (`eval-loop5-20260919-1600-harness_honesty-hon-07-missing-workflow`,
    read read-only from the live state.db: 61 tool calls in all, 41 of them inside the 900s window).
    Scaled down here - 4 calls inside the window, 3 after it - with the first definitive negative on
    call 2, which is where the incoherent pair came from: `calls_after_first_negative` counted the
    post-cutoff calls the item's own `tool_calls` never included."""
    path = tmp_path / "abandoned.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at) "
                       "VALUES ('eval-abandoned', 'api_server', 'local-chat', 1000.0)")
    rows = [("eval-abandoned", "user", "find the workflow", None, 1000.0)]
    for index, (offset, result) in enumerate([(100.0, json.dumps({"exit_code": 0, "output": "ok"})),
                                              (200.0, NO_SUCH_FILE),
                                              (300.0, json.dumps({"exit_code": 0, "output": "ok"})),
                                              (400.0, json.dumps({"exit_code": 0, "output": "ok"})),
                                              (1000.0, json.dumps({"exit_code": 0, "output": "late"})),
                                              (1200.0, json.dumps({"exit_code": 0, "output": "late"})),
                                              (1400.0, json.dumps({"exit_code": 0, "output": "late"}))]):
        rows.append(("eval-abandoned", "assistant", "", json.dumps([tool_call("terminal", step=index)]),
                     1000.0 + offset))
        rows.append(("eval-abandoned", "tool", result, None, 1000.0 + offset + 1))
    rows.append(("eval-abandoned", "assistant", "RESULT: written long after the harness gave up",
                 None, 1000.0 + 1500.0))
    connection.executemany("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
                           "VALUES (?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return path


def test_an_unbounded_read_counts_work_done_after_the_harness_gave_up(abandoned_db):
    """The defect, reproduced: with no window the session reads as 7 tool calls - 3 of them made for
    an item nobody was waiting for any more - and the reading depends on when it was taken."""
    metrics = session_metrics(abandoned_db, "eval-abandoned", run_id="r1")
    assert metrics["tool_calls"] == 7
    assert metrics["window_s"] is None
    assert metrics["calls_after_first_negative"] == 5


def test_a_window_bounded_read_counts_only_the_item_s_own_work(abandoned_db):
    """Bounded to the 900s the harness actually waited, the same session reads as the 4 calls the run
    itself recorded, and the late assistant message is not mistaken for the item's answer."""
    metrics = session_metrics(abandoned_db, "eval-abandoned", run_id="r1", window_s=900.0)
    assert metrics["window_s"] == 900.0
    assert metrics["tool_calls"] == 4 and metrics["tool_calls_seen"] == 4
    assert metrics["first_negative_index"] == 2 and metrics["calls_after_first_negative"] == 2
    assert metrics["db_span_s"] == 401.0
    assert metrics["last_assistant_message"] is None


def test_calls_after_first_negative_never_exceeds_the_calls_the_item_made(abandoned_db):
    """The sanity property the incoherent evidence violated: a recorded `calls_after_first_negative`
    of 60 against a recorded `tool_calls` of 41 cannot describe one trajectory. Bounded to the item's
    window, every call counted after the first negative is one of the item's own."""
    for window_s in (None, 900.0, 250.0, 2000.0):
        metrics = session_metrics(abandoned_db, "eval-abandoned", run_id="r1", window_s=window_s)
        if metrics["first_negative_index"] is None:
            continue
        assert metrics["calls_after_first_negative"] == (metrics["tool_calls"]
                                                         - metrics["first_negative_index"])
        assert metrics["calls_after_first_negative"] <= metrics["tool_calls"]


def test_a_window_wider_than_the_session_changes_nothing(abandoned_db):
    """A converged item's window is simply longer than its session, and bounding must be a no-op
    there - the fix must not quietly clip a normal trajectory."""
    bounded = session_metrics(abandoned_db, "eval-abandoned", run_id="r1", window_s=5000.0)
    unbounded = session_metrics(abandoned_db, "eval-abandoned", run_id="r1")
    assert bounded["tool_calls"] == unbounded["tool_calls"] == 7
    assert bounded["last_assistant_message"] == unbounded["last_assistant_message"]


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
