"""Shared solver for the harness suites: one Hermes API-server turn per item, then trajectory metrics
from state.db. Scoring is suite-specific (harness_ops: out-of-band check; harness_honesty:
classification of the claim).

session_id_for / session_key_for / read_trajectory / call_hermes are thin ctx-shaped wrappers around
ordo_evals.hermes_turn (a pure module with no inspect_ai import - see its docstring); the actual
per-item-budget and E10 timeout-recovery logic lives there and is unit-tested there directly.
Imported under an alias below so it doesn't collide with this module's own `hermes_turn` solver."""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from inspect_ai.model import ModelOutput
from inspect_ai.solver import Generate, TaskState, solver

from ordo_evals import checks
from ordo_evals import hermes_turn as _hermes_turn
from ordo_evals.hermes_client import HermesTurn
from ordo_evals.prompts import EVAL_SYSTEM_PROMPT
from ordo_evals.suites import common

session_id_for = _hermes_turn.session_id_for
session_key_for = _hermes_turn.session_key_for


async def read_trajectory(ctx: common.SuiteContext, session_id: str) -> dict[str, Any]:
    return await _hermes_turn.read_trajectory(ctx.settings.hermes_state_db, session_id)


async def call_hermes(ctx: common.SuiteContext, *, prompt: str, system: str | None, session_id: str,
                      session_key: str, model: str) -> tuple[HermesTurn, dict[str, Any]]:
    return await _hermes_turn.call_hermes(
        ctx.hermes, prompt=prompt, system=system, session_id=session_id, session_key=session_key, model=model,
        state_db=ctx.settings.hermes_state_db, budget_s=ctx.settings.hermes_item_budget_s)


@solver
def hermes_turn(ctx: common.SuiteContext, suite: str, *, check_precondition: bool):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        item = state.metadata["item"]
        context = checks.item_context(ctx.run_id, item["id"])
        state.metadata["context"] = context
        if check_precondition:
            holds, detail = await asyncio.to_thread(checks.precondition_holds, item, context, ctx.probes)
            state.metadata["precondition"] = {"holds": holds, "detail": detail}
            if not holds:
                state.output = ModelOutput.from_content(model="hermes", content="")
                state.completed = True
                return state
        await asyncio.to_thread(checks.run_setup, item, context, ctx.probes)
        session_id = session_id_for(ctx.run_id, suite, item["id"])
        print(f"[{suite}] {item['id']}: asking Hermes (session {session_id})", flush=True)
        turn, traj = await call_hermes(ctx, prompt=checks.build_prompt(item, context), system=EVAL_SYSTEM_PROMPT,
                                       session_id=session_id, session_key=session_key_for(ctx.run_id),
                                       model=ctx.hermes_model_name)
        state.metadata["turn"] = dataclasses.asdict(turn)
        if traj:
            state.metadata["trajectory"] = traj
        state.output = ModelOutput.from_content(model="hermes", content=turn.text or "")
        return state

    return solve
