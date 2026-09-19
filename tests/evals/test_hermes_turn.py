"""E10 (round-4 fix): the per-item wall-clock budget and its state.db timeout-recovery path
(ordo_evals.hermes_turn.call_hermes), against a fake HermesClient and a synthetic sqlite fixture
built to the same schema as Hermes v0.20.0 (see tests/evals/test_state_db_readers.py)."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from ordo_evals import hermes_turn as hermes_turn_module
from ordo_evals.hermes_client import HermesTurn
from ordo_evals.hermes_turn import call_hermes, has_usable_output, partial_answer

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, parent_session_id TEXT,
                       started_at REAL NOT NULL, ended_at REAL, input_tokens INTEGER DEFAULT 0,
                       output_tokens INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
                       content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL);
"""


@pytest.fixture
def state_db(tmp_path):
    """A session that was alive and had reached an answer (98/19-tool-turn evidence from the brief),
    at session id "eval-alive"."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-alive', 'api_server', 'local-chat', 1000.0, 900, 400)")
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, ?, ?, ?, ?)",
        [("eval-alive", "user", "do the thing", None, 1000.0),
         ("eval-alive", "assistant", "", json.dumps([{"id": "c1", "type": "function",
                                                       "function": {"name": "terminal", "arguments": "{}"}}]), 1001.0),
         ("eval-alive", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 1002.0),
         ("eval-alive", "assistant", "RESULT: it worked", None, 1900.0)])
    connection.commit()
    connection.close()
    return path


class FakeHermesClient:
    """A HermesClient stand-in: either returns `turn` immediately, or sleeps `hang_s` first (to
    exercise call_hermes's own asyncio.wait_for budget, not HermesClient's internal httpx timeout)."""

    def __init__(self, turn: HermesTurn | None = None, hang_s: float | None = None):
        self._turn = turn
        self._hang_s = hang_s

    async def chat(self, *, prompt, system, session_id, session_key, model):
        if self._hang_s is not None:
            await asyncio.sleep(self._hang_s)
        return self._turn


async def test_the_per_item_budget_firing_recovers_the_session_and_scores_a_real_result(state_db):
    """The item budget (not HermesClient's own httpx timeout) fires first: call_hermes must catch
    that itself, then recover the ALIVE session from state.db - the exact iteration-2 evidence shape
    (a ReadTimeout with no trajectory, even though Hermes was still working the turn)."""
    client = FakeHermesClient(hang_s=5.0)  # would only return after the budget below has expired
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive",
                                   session_key="k", model="local-chat", state_db=state_db, budget_s=0.05)
    assert turn.error_kind == "timeout"
    assert turn.budget_exceeded is True
    assert traj["found"] is True
    # E10: the last assistant message is recovered and backfilled onto the turn's text, so the item
    # has something to check/judge instead of an empty reply.
    assert turn.text == "RESULT: it worked"
    assert traj["last_assistant_message"] == "RESULT: it worked"


async def test_a_transport_level_timeout_from_the_client_also_recovers_the_session(state_db):
    """hermes_client.HermesClient itself can also return error_kind == "timeout" (its own httpx-level
    ReadTimeout, E10's hermes_client.py fix) - call_hermes must treat that identically to its own
    budget firing, not only the asyncio.wait_for path above."""
    timeout_turn = HermesTurn(None, None, "eval-alive", 12.3, error="ReadTimeout", error_kind="timeout",
                              budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive",
                                   session_key="k", model="local-chat", state_db=state_db, budget_s=30.0)
    assert turn.error_kind == "timeout"
    assert traj["found"] is True
    assert turn.text == "RESULT: it worked"


async def test_a_timed_out_session_state_db_has_no_record_of_is_never_backfilled(tmp_path, monkeypatch):
    """If state.db has NOTHING for the session id (the empty db below), there is nothing to recover:
    turn.text stays None and the trajectory reports found: False - the caller (suites/harness.py's
    scorers) is what decides such a case still counts as an infra error, but call_hermes itself never
    invents an answer."""
    # read_trajectory retries a few times with a real sleep (state.db rows can lag the HTTP response);
    # not relevant to what this test asserts, so collapse it to keep the suite fast.
    monkeypatch.setattr(hermes_turn_module, "TRAJECTORY_DELAY_S", 0)
    empty_db = tmp_path / "empty.db"
    connection = sqlite3.connect(empty_db)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    timeout_turn = HermesTurn(None, None, "eval-ghost", 900.0, error="budget exceeded", error_kind="timeout",
                              budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-ghost",
                                   session_key="k", model="local-chat", state_db=empty_db, budget_s=30.0)
    assert turn.error_kind == "timeout"
    assert turn.text is None
    assert traj["found"] is False


async def test_a_timed_out_session_alive_with_no_content_yet_recovers_nothing_but_is_still_found(tmp_path):
    """E14 (round-5 fix): the actual loop3-20260918-1644 shape for six items - the session row exists
    and Hermes was still mid-turn (every assistant message so far was a content-less tool call), so
    the budget firing recovers `found: True` but `last_assistant_message: None`. This is NOT the
    no-record-at-all case above: the caller must be able to tell "genuinely still working, nothing
    written yet" (did_not_converge, no usable output - see hermes_turn.has_usable_output) apart from
    "no session at all" (infra_error)."""
    path = tmp_path / "mid-turn.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-mid-turn', 'api_server', 'local-chat', 1000.0, 300, 0)")
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, ?, ?, ?, ?)",
        [("eval-mid-turn", "user", "do the thing", None, 1000.0),
         ("eval-mid-turn", "assistant", "", json.dumps([{"id": "c1", "type": "function",
                                                          "function": {"name": "terminal", "arguments": "{}"}}]),
          1001.0),
         ("eval-mid-turn", "tool", json.dumps({"exit_code": 0, "output": "still working"}), None, 1002.0)])
    connection.commit()
    connection.close()
    timeout_turn = HermesTurn(None, None, "eval-mid-turn", 900.0, error="budget exceeded", error_kind="timeout",
                              budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-mid-turn",
                                   session_key="k", model="local-chat", state_db=path, budget_s=30.0)
    assert turn.error_kind == "timeout"
    assert traj["found"] is True
    assert traj["last_assistant_message"] is None
    assert turn.text is None
    assert has_usable_output(turn.text) is False


async def test_a_transport_failure_never_touches_state_db(tmp_path):
    """Connection refused / 401 / 5xx stay infra errors and skip state.db entirely - the trajectory
    dict must be exactly {} (the caller distinguishes this from `traj={"found": False, ...}` above)."""
    transport_turn = HermesTurn(None, None, "s1", 0.1, error="ConnectError", error_kind="transport")
    client = FakeHermesClient(turn=transport_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="s1", session_key="k",
                                   model="local-chat", state_db=tmp_path / "does-not-exist.db", budget_s=30.0)
    assert turn.error_kind == "transport"
    assert traj == {}


async def test_a_normal_reply_is_not_touched_by_the_backfill(state_db):
    """A normal 200 reply already has text; the recovery/backfill logic must never overwrite it, and
    the trajectory is still attached (unchanged behavior from before E10)."""
    ok_turn = HermesTurn(200, "the model's own answer", "eval-alive", 4.2)
    client = FakeHermesClient(turn=ok_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                   model="local-chat", state_db=state_db, budget_s=30.0)
    assert turn.error_kind is None
    assert turn.text == "the model's own answer"
    assert traj["found"] is True


# ── has_usable_output / partial_answer (E14, round-5 fix): the judge queue-exclusion rule ──────────

@pytest.mark.parametrize(("text", "usable"), [
    ("RESULT: 42", True), ("some fragment of an in-progress answer", True),
    (None, False), ("", False), ("   \n\t  ", False),
])
def test_has_usable_output(text, usable):
    assert has_usable_output(text) is usable


def test_a_did_not_converge_item_with_no_recovered_text_has_nothing_to_queue():
    """The loop3-20260918-1644 evidence: a did_not_converge item whose state.db recovery found no
    assistant text at all (the budget fired well before Hermes wrote anything with content) must
    never be treated as a partial answer - there is nothing for a human to grade."""
    assert has_usable_output(None) is False
    assert partial_answer(did_not_converge=True, text=None) is False
    assert partial_answer(did_not_converge=True, text="") is False


def test_a_did_not_converge_item_with_a_recovered_fragment_is_partial():
    """The loop3-20260918-1644 evidence: two harness_domain items recovered a short mid-task remark
    (a message written before the budget fired, not the agent's real final answer, which the session
    went on to produce much later) - still worth a judge's grade, but marked partial."""
    assert partial_answer(did_not_converge=True, text="a mid-task remark, not the real final answer") is True


def test_a_converged_items_answer_is_never_partial_even_with_text():
    """partial_answer is specifically about a did_not_converge turn cut short - a normal completed
    turn's answer is never marked partial no matter its content."""
    assert partial_answer(did_not_converge=False, text="a completed, ordinary answer") is False
