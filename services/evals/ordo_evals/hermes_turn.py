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

from ordo_evals import trajectory
from ordo_evals.hermes_client import HermesClient, HermesTurn
from ordo_evals.ids import safe_token

# state.db rows are written during the turn; allow a moment for the final write to land.
TRAJECTORY_ATTEMPTS = 4
TRAJECTORY_DELAY_S = 1.5


def session_id_for(run_id: str, suite: str, item_id: str) -> str:
    return f"eval-{safe_token(run_id, 48)}-{safe_token(suite, 24)}-{safe_token(item_id, 64)}"


def session_key_for(run_id: str) -> str:
    return f"ordo-evals-{safe_token(run_id, 48)}"


async def read_trajectory(state_db: str | Path, session_id: str) -> dict[str, Any]:
    for attempt in range(TRAJECTORY_ATTEMPTS):
        try:
            metrics = await asyncio.to_thread(trajectory.session_metrics, state_db, session_id)
        except Exception as exc:  # an unreadable state.db must not lose the item; record why
            return {"found": False, "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}
        if metrics.get("found") or attempt == TRAJECTORY_ATTEMPTS - 1:
            return metrics
        await asyncio.sleep(TRAJECTORY_DELAY_S)
    return {"found": False, "session_id": session_id}


async def call_hermes(hermes: HermesClient, *, prompt: str, system: str | None, session_id: str,
                      session_key: str, model: str, state_db: str | Path,
                      budget_s: float) -> tuple[HermesTurn, dict[str, Any]]:
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
    traj = await read_trajectory(state_db, turn.session_id)
    traj["wall_time_s"] = turn.wall_time_s
    if turn.error_kind == "timeout" and not turn.text and traj.get("last_assistant_message"):
        turn = dataclasses.replace(turn, text=traj["last_assistant_message"])
    return turn, traj
