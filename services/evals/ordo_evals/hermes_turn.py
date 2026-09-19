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


def session_id_for(run_id: str, suite: str, item_id: str) -> str:
    return f"eval-{safe_token(run_id, 48)}-{safe_token(suite, 24)}-{safe_token(item_id, 64)}"


def session_key_for(run_id: str) -> str:
    return f"ordo-evals-{safe_token(run_id, 48)}"


async def read_trajectory(state_db: str | Path, session_id: str, *, run_id: str) -> dict[str, Any]:
    """`run_id` is this item's own run, needed by the E19 replay reading - see trajectory.py."""
    for attempt in range(TRAJECTORY_ATTEMPTS):
        try:
            metrics = await asyncio.to_thread(trajectory.session_metrics, state_db, session_id, run_id=run_id)
        except Exception as exc:  # an unreadable state.db must not lose the item; record why
            return {"found": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}
        if metrics.get("found") or attempt == TRAJECTORY_ATTEMPTS - 1:
            return metrics
        await asyncio.sleep(TRAJECTORY_DELAY_S)
    return {"found": False, "session_id": session_id}


async def call_hermes(hermes: HermesClient, *, prompt: str, system: str | None, session_id: str,
                      session_key: str, model: str, state_db: str | Path, run_id: str,
                      budget_s: float, probes: Any = None,
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
    traj = await read_trajectory(state_db, turn.session_id, run_id=run_id)
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
