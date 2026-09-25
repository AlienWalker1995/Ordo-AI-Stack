"""E10 (round-4 fix): the per-item wall-clock budget and its state.db timeout-recovery path
(ordo_evals.hermes_turn.call_hermes), against a fake HermesClient and a synthetic sqlite fixture
built to the same schema as Hermes v0.20.0 (see tests/evals/test_state_db_readers.py)."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from ordo_evals import gpu_guard
from ordo_evals import hermes_turn as hermes_turn_module
from ordo_evals.checks import ProbeError
from ordo_evals.hermes_client import HermesTurn
from ordo_evals.hermes_turn import call_hermes, has_usable_output, judgeable

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
    at session id "eval-alive".

    Its clock is scaled to the sub-second budgets these tests use: every row lands within 30ms of
    `started_at`, because since E22 (round-10 fix) a trajectory is read bounded to the seconds the
    harness actually waited, and a test that gives up after 50ms must not be asserting recovery of a
    message Hermes wrote fifteen minutes later. A session that keeps writing AFTER the cutoff is its
    own fixture (`abandoned_db` below), where that is the point."""
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-alive', 'api_server', 'local-chat', 1000.0, 900, 400)")
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, ?, ?, ?, ?)",
        [("eval-alive", "user", "do the thing", None, 1000.0),
         ("eval-alive", "assistant", "", json.dumps(
             [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]), 1000.01),
         ("eval-alive", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 1000.02),
         ("eval-alive", "assistant", "RESULT: it worked", None, 1000.03)])
    connection.commit()
    connection.close()
    return path


class FakeHermesClient:
    """A HermesClient stand-in: either returns `turn` immediately, or sleeps `hang_s` first (to
    exercise call_hermes's own asyncio.wait_for budget, not HermesClient's internal httpx timeout).

    `active_work` (E23) is the sequence of answers its `active_agent_work()` gives, one per call -
    Hermes's own in-flight agent count, which is how the harness sees the abandoned turn let go of
    the model slot. The last value repeats once the sequence is exhausted; the default (0) is an
    agent that is already idle, so a test that is not about the wait never waits."""

    def __init__(self, turn: HermesTurn | None = None, hang_s: float | None = None,
                 active_work: list[int | None] | None = None):
        self._turn = turn
        self._hang_s = hang_s
        self._active_work = list(active_work) if active_work else [0]
        self.active_work_calls = 0

    async def chat(self, *, prompt, system, session_id, session_key, model):
        if self._hang_s is not None:
            await asyncio.sleep(self._hang_s)
        return self._turn

    async def active_agent_work(self):
        index = min(self.active_work_calls, len(self._active_work) - 1)
        self.active_work_calls += 1
        return self._active_work[index]


async def test_the_per_item_budget_firing_recovers_the_session_and_scores_a_real_result(state_db):
    """The item budget (not HermesClient's own httpx timeout) fires first: call_hermes must catch
    that itself, then recover the ALIVE session from state.db - the exact iteration-2 evidence shape
    (a ReadTimeout with no trajectory, even though Hermes was still working the turn)."""
    client = FakeHermesClient(hang_s=5.0)  # would only return after the budget below has expired
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive",
                                   session_key="k", model="local-chat", state_db=state_db, run_id="r1",
                                   budget_s=0.05, overrun_wait_s=30.0)
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
                                   session_key="k", model="local-chat", state_db=state_db, run_id="r1",
                                   budget_s=30.0, overrun_wait_s=30.0)
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
                                   session_key="k", model="local-chat", state_db=empty_db, run_id="r1",
                                   budget_s=30.0, overrun_wait_s=30.0)
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
                                   session_key="k", model="local-chat", state_db=path, run_id="r1",
                                   budget_s=30.0, overrun_wait_s=30.0)
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
                                   model="local-chat", state_db=tmp_path / "does-not-exist.db", run_id="r1",
                                   budget_s=30.0, overrun_wait_s=30.0)
    assert turn.error_kind == "transport"
    assert traj == {}


async def test_a_normal_reply_is_not_touched_by_the_backfill(state_db):
    """A normal 200 reply already has text; the recovery/backfill logic must never overwrite it, and
    the trajectory is still attached (unchanged behavior from before E10)."""
    ok_turn = HermesTurn(200, "the model's own answer", "eval-alive", 4.2)
    client = FakeHermesClient(turn=ok_turn)
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                   model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                   overrun_wait_s=30.0)
    assert turn.error_kind is None
    assert turn.text == "the model's own answer"
    assert traj["found"] is True


# ── has_usable_output / judgeable (E14, round 5; E18, round 7): the judge queue-exclusion rule ─────

@pytest.mark.parametrize(("text", "usable"), [
    ("RESULT: 42", True), ("some fragment of an in-progress answer", True),
    (None, False), ("", False), ("   \n\t  ", False),
])
def test_has_usable_output(text, usable):
    assert has_usable_output(text) is usable


def test_a_converged_item_with_a_real_answer_is_judgeable():
    assert judgeable(infra_error=False, did_not_converge=False, text="RESULT: 42") is True


def test_a_did_not_converge_item_with_no_recovered_text_has_nothing_to_queue():
    """The loop3-20260918-1644 evidence: a did_not_converge item whose state.db recovery found no
    assistant text at all (the budget fired well before Hermes wrote anything with content) has
    nothing for a human to grade."""
    assert has_usable_output(None) is False
    assert judgeable(infra_error=False, did_not_converge=True, text=None) is False


def test_a_did_not_converge_item_with_a_recovered_fragment_is_still_not_judgeable():
    """E18 (round-7 fix): the round-5 gate queued exactly this - loop4b-20260919-1048's
    pd-a50af3dca757 and hon-07-missing-workflow both hit the 900s budget, and because the recovery
    caught a mid-thought sentence a human judge was asked to grade the agent's PLAN. Whether the
    recovery window happened to catch text is harness timing, not an answer: a non-convergence is a
    non-convergence, never a judged failure."""
    assert has_usable_output("I will start by searching the vault for the note") is True
    assert judgeable(infra_error=False, did_not_converge=True,
                     text="I will start by searching the vault for the note") is False


def test_an_infra_error_is_never_judgeable():
    """The runner could not reach Hermes at all: there is no reply, and the item is excluded from
    every metric - it must not reach the judge either."""
    assert judgeable(infra_error=True, did_not_converge=False, text="a reply of some kind") is False


# ── E15 (round-6 fix): per-item served_model via gpu_guard ──────────────────────────────────────

IDLE_STATUS = {"manifest": {}, "gpu": {"state": "idle", "leased": False, "running": [], "queued": [], "evicted_residents": {}}}
LEASED_STATUS = {"manifest": {}, "gpu": {"state": "busy", "leased": True, "running": [{"id": "gate-comfyui"}], "queued": [],
                                        "evicted_residents": {}}}


class _FakeProbes:
    def __init__(self, status=None, fail=False):
        self._status = status
        self._fail = fail

    def ops_status(self):
        if self._fail:
            raise ProbeError("ops-controller unreachable")
        return self._status


async def test_no_probes_means_no_served_model_is_recorded(state_db):
    """Backward compatible default: a caller that passes no `probes` (every existing call site
    before this fix, and every test above) gets exactly the trajectory shape it always did."""
    ok_turn = HermesTurn(200, "an answer", "eval-alive", 4.2)
    client = FakeHermesClient(turn=ok_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0, overrun_wait_s=30.0)
    assert "served_model" not in traj


async def test_served_model_is_the_gpu_model_when_the_scheduler_is_clear(state_db):
    ok_turn = HermesTurn(200, "an answer", "eval-alive", 4.2)
    client = FakeHermesClient(turn=ok_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0, overrun_wait_s=30.0,
                                probes=_FakeProbes(IDLE_STATUS), gpu_served_model="qwen-gpu")
    assert traj["served_model"] == "qwen-gpu"


async def test_served_model_is_the_cpu_fallback_sentinel_when_the_gpu_is_leased(state_db):
    """The iteration-4 shape: a normal-looking 200 reply, but the scheduler shows the GPU leased at
    the moment the turn finished - this is exactly the case items.jsonl needs to carry visibly."""
    ok_turn = HermesTurn(200, "an answer served slowly by the CPU fallback", "eval-alive", 250.0)
    client = FakeHermesClient(turn=ok_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=300.0, overrun_wait_s=30.0,
                                probes=_FakeProbes(LEASED_STATUS), gpu_served_model="qwen-gpu")
    assert traj["served_model"] == gpu_guard.CPU_FALLBACK_BACKEND


async def test_served_model_is_still_checked_for_a_recovered_timeout(state_db):
    """E10's timeout-recovery path and E15's served_model check are independent - a did_not_converge
    item (recovered from state.db) still gets a served_model, since that is exactly the shape
    (slow CPU fallback -> item hits the per-item budget) this check exists to surface."""
    timeout_turn = HermesTurn(None, None, "eval-alive", 12.3, error="ReadTimeout", error_kind="timeout",
                              budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0, overrun_wait_s=30.0,
                                probes=_FakeProbes(LEASED_STATUS), gpu_served_model="qwen-gpu")
    assert traj["found"] is True
    assert traj["served_model"] == gpu_guard.CPU_FALLBACK_BACKEND


async def test_served_model_is_never_checked_for_a_transport_failure(tmp_path):
    """A transport failure means no turn happened at all - there is nothing to attribute to a
    backend, and traj must stay exactly {} (the existing infra_error contract, unchanged by E15)."""
    transport_turn = HermesTurn(None, None, "s1", 0.1, error="ConnectError", error_kind="transport")
    client = FakeHermesClient(turn=transport_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="s1", session_key="k",
                                model="local-chat", state_db=tmp_path / "does-not-exist.db", run_id="r1",
                                budget_s=30.0, overrun_wait_s=30.0, probes=_FakeProbes(IDLE_STATUS),
                                gpu_served_model="qwen-gpu")
    assert traj == {}


async def test_served_model_is_unknown_with_a_note_when_ops_controller_is_unreachable(state_db):
    ok_turn = HermesTurn(200, "an answer", "eval-alive", 4.2)
    client = FakeHermesClient(turn=ok_turn)
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0, overrun_wait_s=30.0,
                                probes=_FakeProbes(fail=True), gpu_served_model="qwen-gpu")
    assert traj["served_model"] == gpu_guard.UNKNOWN_BACKEND
    assert "could not check" in traj["served_model_note"]


# ── E23 (round-10 fix): an abandoned item must not keep the model slot ───────────

@pytest.fixture
def abandoned_db(tmp_path):
    """The recorded shape of a budget-exceeded item: the harness stopped waiting at 0.05s of this
    fixture's scaled clock, Hermes carried on well past it - the real
    `eval-loop5-20260919-1600-harness_honesty-hon-07-missing-workflow` ran 1773s against a 900s
    budget, 61 tool calls of which only 41 were inside the window."""
    path = tmp_path / "abandoned.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
                       "VALUES ('eval-abandoned', 'api_server', 'local-chat', 1000.0, 900, 400)")
    call = json.dumps([{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
    connection.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?, ?, ?, ?, ?)",
        [("eval-abandoned", "user", "do the thing", None, 1000.0),
         ("eval-abandoned", "assistant", "", call, 1000.01),
         ("eval-abandoned", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 1000.02),
         # everything below landed AFTER the harness gave up waiting
         ("eval-abandoned", "assistant", "", call, 1000.5),
         ("eval-abandoned", "tool", json.dumps({"exit_code": 0, "output": "ok"}), None, 1000.6),
         ("eval-abandoned", "assistant", "RESULT: found it, eventually", None, 1000.9)])
    connection.commit()
    connection.close()
    return path


async def test_a_budget_exceeded_item_waits_for_hermes_to_release_the_model_slot(state_db, monkeypatch):
    """Concurrency is 1 because every turn shares one llama.cpp slot. Hermes reports two turns in
    flight, then one, then none: call_hermes must not return (and let the next item start) until it
    reads a zero, and must record how long that took. (The real poll interval is IDLE_POLL_S seconds;
    collapsed here the same way the trajectory retry delay is elsewhere in this file.)"""
    monkeypatch.setattr(hermes_turn_module, "IDLE_POLL_S", 0.01)
    timeout_turn = HermesTurn(None, None, "eval-alive", 0.05, error="budget exceeded",
                              error_kind="timeout", budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn, active_work=[2, 1, 0])
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                overrun_wait_s=30.0)
    assert client.active_work_calls == 3
    assert traj["overrun_timed_out"] is False
    assert traj["overrun_s"] >= 0.0


async def test_a_converged_item_never_waits_and_records_no_overrun(state_db):
    """The wait exists for abandoned work only: a turn that returned normally has already released
    the slot, and must not pay a single probe for it."""
    client = FakeHermesClient(turn=HermesTurn(200, "an answer", "eval-alive", 4.2), active_work=[3])
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                overrun_wait_s=30.0)
    assert client.active_work_calls == 0
    assert "overrun_s" not in traj and "overrun_timed_out" not in traj


async def test_a_wait_that_itself_times_out_is_recorded_not_hidden(state_db, monkeypatch):
    """The case that must never be silent: Hermes is STILL working when the bounded wait expires, so
    the next item really does start under contention. The run has to carry that, or the following
    items' numbers look clean while being exactly the confound this measures."""
    monkeypatch.setattr(hermes_turn_module, "IDLE_POLL_S", 0.01)
    timeout_turn = HermesTurn(None, None, "eval-alive", 0.05, error="budget exceeded",
                              error_kind="timeout", budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn, active_work=[1])  # never goes idle
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                overrun_wait_s=0.05)
    assert traj["overrun_timed_out"] is True
    assert traj["overrun_s"] >= 0.05
    assert "still had 1 agent turn(s) in flight" in traj["overrun_note"]


async def test_an_unreadable_readiness_signal_stops_the_wait_instead_of_blocking_on_it(state_db):
    """Hermes not answering /health/detailed is "could not tell", never "the slot is free" and never
    a reason to burn the whole bound: the wait ends at once and says why, so a reader knows this
    item's overrun was not measured rather than believing it was zero."""
    timeout_turn = HermesTurn(None, None, "eval-alive", 0.05, error="budget exceeded",
                              error_kind="timeout", budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn, active_work=[None])
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                overrun_wait_s=30.0)
    assert traj["overrun_timed_out"] is False
    assert "did not report" in traj["overrun_note"]


async def test_wait_for_agent_idle_returns_immediately_when_hermes_is_already_idle():
    client = FakeHermesClient(active_work=[0])
    result = await hermes_turn_module.wait_for_agent_idle(client, timeout_s=30.0)
    assert result == {"overrun_s": result["overrun_s"], "overrun_timed_out": False}
    assert result["overrun_s"] < 1.0


# ── E22 (round-10 fix): the recorded trajectory is the item's own window ─────────

async def test_the_recorded_trajectory_excludes_work_done_after_the_budget_fired(abandoned_db):
    """The item recorded 1 tool call; reading the session unbounded after the wait would record 2 and
    hand the item an answer Hermes only reached once nobody was waiting for it."""
    timeout_turn = HermesTurn(None, None, "eval-abandoned", 0.05, error="budget exceeded",
                              error_kind="timeout", budget_exceeded=True)
    client = FakeHermesClient(turn=timeout_turn, active_work=[0])
    turn, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-abandoned",
                                   session_key="k", model="local-chat", state_db=abandoned_db, run_id="r1",
                                   budget_s=30.0, overrun_wait_s=30.0)
    assert traj["window_s"] == 0.05
    assert traj["tool_calls"] == 1 and traj["tool_calls_seen"] == 1
    assert traj["last_assistant_message"] is None
    assert turn.text is None


async def test_the_window_is_the_seconds_the_harness_actually_waited(state_db):
    """A converged turn is bounded too - by its own round trip, which is wider than its session, so
    the reading is unchanged. The bound is the same rule in both paths (and in backfill-metrics), not
    a special case for timeouts."""
    client = FakeHermesClient(turn=HermesTurn(200, "an answer", "eval-alive", 4.2))
    _, traj = await call_hermes(client, prompt="p", system=None, session_id="eval-alive", session_key="k",
                                model="local-chat", state_db=state_db, run_id="r1", budget_s=30.0,
                                overrun_wait_s=30.0)
    assert traj["window_s"] == 4.2
    assert traj["tool_calls"] == 1 and traj["last_assistant_message"] == "RESULT: it worked"
