"""Pure Hermes-turn orchestration, shared by every harness suite: session ids, reading trajectory
metrics back from state.db, and the per-item wall-clock budget with its E10 (round-4) timeout
recovery. Kept separate from suites/harness.py and suites/harness_domain.py (both import inspect_ai -
services/evals/requirements.txt's eval-framework dependency, available inside the evals container but
not in the root test environment; see tests/requirements.txt and .github/workflows/ci.yml's `pytest`
job) so this logic can be exercised directly in tests/evals, the same way honesty.py, checks.py and
private_dataset.py already are.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from typing import Any

from ordo_evals import gpu_guard, trajectory
from ordo_evals.hermes_client import HermesClient, HermesTurn
from ordo_evals.ids import safe_token

# state.db rows are written during the turn; allow a moment for the final write to land.
TRAJECTORY_ATTEMPTS = 4
TRAJECTORY_DELAY_S = 1.5

# E23: how often `wait_for_agent_idle` asks Hermes whether it is still working. Small relative to the
# overruns it measures (873s and 853s in the recorded evidence), large enough to be free.
IDLE_POLL_S = 5.0


def session_id_for(run_id: str, suite: str, item_id: str) -> str:
    return f"eval-{safe_token(run_id, 48)}-{safe_token(suite, 24)}-{safe_token(item_id, 64)}"


def session_key_for(run_id: str) -> str:
    return f"ordo-evals-{safe_token(run_id, 48)}"


async def read_trajectory(state_db: str | Path, session_id: str, *, run_id: str,
                          window_s: float | None = None) -> dict[str, Any]:
    """`run_id` is this item's own run, needed by the E19 replay reading - see trajectory.py.

    `window_s` (E22) bounds the reading to the seconds the harness actually waited for this item, so
    what is recorded is what the run saw and a later `backfill-metrics` of the same session gets the
    same answer."""
    for attempt in range(TRAJECTORY_ATTEMPTS):
        try:
            metrics = await asyncio.to_thread(trajectory.session_metrics, state_db, session_id,
                                              run_id=run_id, window_s=window_s)
        except Exception as exc:  # an unreadable state.db must not lose the item; record why
            return {"found": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}
        if metrics.get("found") or attempt == TRAJECTORY_ATTEMPTS - 1:
            return metrics
        await asyncio.sleep(TRAJECTORY_DELAY_S)
    return {"found": False, "session_id": session_id}


async def wait_for_agent_idle(hermes: Any, *, timeout_s: float, poll_s: float | None = None) -> dict[str, Any]:
    """Block until Hermes reports no agent work in flight, and report how long that took (E23).

    An item that exceeds its budget is abandoned by the harness, but NOT by Hermes: the two recorded
    `hon-07-missing-workflow` sessions kept working 873s and 853s past the 900s cutoff. Concurrency
    is 1 because every turn shares one llama.cpp slot, so without this wait the next items run
    against an agent that is still generating for an item nobody is waiting for - the harness
    manufacturing exactly the contention its timing instrumentation exists to detect.

    There is no server-side cancel to use instead: see hermes_client.HermesClient's module docstring
    (the only interrupt route is keyed to /v1/runs run ids, which a chat-completions turn does not
    have). So the harness waits, visibly and bounded.

    Returns `{"overrun_s": <seconds waited>, "overrun_timed_out": bool, "overrun_note": str?}`.
      * `overrun_timed_out: True` - `timeout_s` elapsed with Hermes still busy. The next item DOES
        start under contention; recording it is what keeps that readable in the results instead of
        silently poisoning the following items.
      * `overrun_note` - the readiness signal could not be read (Hermes unreachable, an unexpected
        shape). The wait stops immediately rather than blocking on a probe it cannot trust; "could
        not tell" is never reported as "the slot is free".
    """
    poll_s = IDLE_POLL_S if poll_s is None else poll_s  # read at call time, so tests can collapse it
    started = time.monotonic()
    while True:
        active = await hermes.active_agent_work()
        elapsed = round(time.monotonic() - started, 3)
        if active is None:
            return {"overrun_s": elapsed, "overrun_timed_out": False,
                    "overrun_note": "Hermes did not report its in-flight agent work; did not wait"}
        if active <= 0:
            return {"overrun_s": elapsed, "overrun_timed_out": False}
        if elapsed >= timeout_s:
            return {"overrun_s": elapsed, "overrun_timed_out": True,
                    "overrun_note": f"Hermes still had {active} agent turn(s) in flight after "
                                    f"{timeout_s}s; the next item starts under contention"}
        await asyncio.sleep(min(poll_s, timeout_s - elapsed))


async def call_hermes(hermes: HermesClient, *, prompt: str, system: str | None, session_id: str,
                      session_key: str, model: str, state_db: str | Path, run_id: str,
                      budget_s: float, overrun_wait_s: float, probes: Any = None,
                      gpu_served_model: str = "") -> tuple[HermesTurn, dict[str, Any]]:
    """One Hermes turn, bounded by `budget_s` (settings.Settings.hermes_item_budget_s -
    EVALS_HERMES_ITEM_BUDGET_S, default 900s), well inside the client's own transport-level timeout.

    On ANY timeout - this budget firing, or the client's own transport-level one
    (hermes_client.HermesTurn.error_kind == "timeout" either way) - the session is looked up in
    Hermes's state.db by `session_id` (the runner already set it before the call, via
    session_id_for). If found, the trajectory and the session's last assistant message are recovered,
    so the item is scored a real result (did_not_converge - a harness-level stopping-rule failure)
    instead of being thrown away as an infra error, which is what happened before this fix even
    though the Hermes session was often still alive and had reached an answer. A genuine transport
    failure (connection refused, 401, 5xx - error_kind == "transport") is the only case that stays an
    infra error; that path never touches state.db.

    E15 (round-6 fix): when `probes` is given, `traj["served_model"]` is set from
    `gpu_guard.served_model_for_item` right after the turn (or its timeout) resolves - the same
    ops-controller GPU-lease check the preflight/mid-run guard uses, checked here per item because
    Hermes's own response carries no genuine per-turn served-backend signal (see gpu_guard.py's
    module docstring for why). Skipped for a transport failure (no turn happened at all - nothing to
    attribute to a backend) and when `probes` is None (a caller that doesn't have one, e.g. a test).

    E23 (round-10 fix): when the budget fires, the harness stops waiting but Hermes does not stop
    working, and concurrency is 1 because every turn shares one llama.cpp slot. So a budget-exceeded
    item is followed by `wait_for_agent_idle` (bounded by `overrun_wait_s`) before this returns, and
    the seconds spent there are recorded on the trajectory as `overrun_s` / `overrun_timed_out` -
    the run-level total lands in summary.json's `contention` block (timing.contention). The
    trajectory is then read for the item's OWN window only (E22, `window_s=turn.wall_time_s`), so
    waiting for the abandoned work to end never adds that work to the item's numbers.

    Returns (turn, trajectory): trajectory is {} when error_kind == "transport".
    """
    started = time.monotonic()
    try:
        turn = await asyncio.wait_for(
            hermes.chat(prompt=prompt, system=system, session_id=session_id, session_key=session_key, model=model),
            timeout=budget_s)
    except TimeoutError:
        elapsed = round(time.monotonic() - started, 3)
        turn = HermesTurn(None, None, session_id, elapsed, error=f"per-item budget of {budget_s}s exceeded",
                          error_kind="timeout", budget_exceeded=True)
    if turn.error_kind == "transport":
        return turn, {}
    overrun: dict[str, Any] = {}
    if turn.error_kind == "timeout":
        print(f"[ordo-evals] {turn.session_id}: budget exceeded after {turn.wall_time_s}s; waiting up to "
              f"{overrun_wait_s}s for Hermes to release the model slot", flush=True)
        overrun = await wait_for_agent_idle(hermes, timeout_s=overrun_wait_s)
        print(f"[ordo-evals] {turn.session_id}: abandoned work held the slot a further "
              f"{overrun['overrun_s']}s{' (WAIT TIMED OUT)' if overrun['overrun_timed_out'] else ''}"
              f"{'; ' + overrun['overrun_note'] if overrun.get('overrun_note') else ''}", flush=True)
    traj = await read_trajectory(state_db, turn.session_id, run_id=run_id, window_s=turn.wall_time_s)
    traj.update(overrun)
    traj["wall_time_s"] = turn.wall_time_s
    if turn.error_kind == "timeout" and not turn.text and traj.get("last_assistant_message"):
        turn = dataclasses.replace(turn, text=traj["last_assistant_message"])
    if probes is not None:
        served_model, note = gpu_guard.served_model_for_item(probes, gpu_served_model=gpu_served_model)
        traj["served_model"] = served_model
        if note:
            traj["served_model_note"] = note
    return turn, traj


def has_usable_output(text: str | None) -> bool:
    """True when `text` is a real reply worth showing a human judge - not None, empty or whitespace.

    Used through `judgeable` below (E18, round-7 fix), which adds the non-convergence rule the
    round-5 gate was missing.

    E14 (round-5 fix): a budget-exceeded item can recover nothing at all - state.db had a session but
    no assistant message had any content yet at read time (the loop3-20260918-1644 evidence: six
    items where the wall-clock budget fired 17s to over 1900s before Hermes's next real content-
    bearing message landed, well outside read_trajectory's few-second retry window). Before this fix,
    every harness_honesty/harness_domain suite queued such an item for the judge anyway (empty
    `output`, nothing to read) instead of treating it as the did_not_converge result it is.
    """
    return bool(text and text.strip())


def judgeable(*, infra_error: bool, did_not_converge: bool, text: str | None) -> bool:
    """Whether an item may go to the judge queue at all (E18, round-7 fix).

    A NON-CONVERGED item never may. Round 5 gated the queue on whether the recovered text happened to
    be empty (`has_usable_output` alone), which let through any budget-exceeded item whose state.db
    recovery caught a mid-thought sentence: in loop4b-20260919-1048, `pd-a50af3dca757`
    (harness_domain) and `hon-07-missing-workflow` (harness_honesty) both hit the 900s budget and both
    reached a human judge, who spent time grading a sentence in which the agent was still describing
    what it planned to do. Whether the recovery window happened to catch a fragment is a property of
    the harness's timing, not of the answer - and a non-convergence is a non-convergence, counted by
    `did_not_converge_rate`, never a judged failure that drags the judge metrics down with it.

    `summary._converged` applies exactly this rule to the denominators, so what is graded and what is
    counted stay the same set of items.
    """
    return not infra_error and not did_not_converge and has_usable_output(text)
