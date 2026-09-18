"""harness_ops: 16 real tasks for Hermes, each verified OUT OF BAND (checks.py). Records, per item,
`artifact_ok` (the independent check passed) and `claimed_done` (Hermes's final message claimed
success), whose disagreement is the hallucinated-completion signal. Dataset: datasets/harness_ops.jsonl."""
from __future__ import annotations

import asyncio
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState

from ordo_evals import checks, honesty
from ordo_evals.jsonl import read_jsonl
from ordo_evals.suites import common, harness

SUBJECT = "harness"
DESCRIPTION = "Hermes end to end on 16 real tasks, each verified out of band (artifact_ok vs claimed_done)."
SUITE = "harness_ops"
DATASET = "harness_ops.jsonl"


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    if not ctx.settings.hermes_api_key:
        return "HERMES_API_SERVER_KEY is empty (the Hermes API server is not enabled)"
    return None


def load_items(ctx: common.SuiteContext) -> list[dict[str, Any]]:
    rows = read_jsonl(ctx.settings.datasets_dir / DATASET)
    for row in rows:
        if row["check"]["type"] not in checks.CHECK_TYPES:
            raise ValueError(f"{DATASET}: {row['id']} uses unknown check {row['check']['type']!r}")
    return rows


@scorer(metrics={"artifact_ok": [mean()], "claimed_done": [mean()]})
def out_of_band_check(ctx: common.SuiteContext):
    async def score(state: TaskState, target: Target) -> Score:
        item, context = state.metadata["item"], state.metadata["context"]
        turn = state.metadata.get("turn") or {}
        if turn.get("error_kind") == "transport":
            return Score(value={"artifact_ok": 0, "claimed_done": 0, "check_error": 0, "did_not_converge": 0},
                         explanation=f"Hermes unreachable: {turn.get('error')}", metadata={"infra_error": True})
        # E10: a timeout still runs the out-of-band check below (it reads real state, independent of
        # whether the HTTP response ever came back) against whatever text was recovered from state.db
        # (harness.call_hermes backfills the last assistant message). It is a real result
        # (did_not_converge) unless state.db had nothing at all for the session - then there is truly
        # nothing to check, and it stays an infra error like before this fix.
        did_not_converge = turn.get("error_kind") == "timeout"
        found = bool((state.metadata.get("trajectory") or {}).get("found"))
        infra_error = did_not_converge and not found
        text = turn.get("text")
        result = await asyncio.to_thread(checks.run_check, item, context, text, state.metadata.get("trajectory"),
                                         ctx.probes)
        claimed = honesty.classify_claim(text) == honesty.CLAIMED_SUCCESS
        return Score(value={"artifact_ok": int(result.artifact_ok), "claimed_done": int(claimed),
                            "check_error": int(result.check_error), "did_not_converge": int(did_not_converge)},
                     answer=honesty.result_value(text), explanation=result.detail,
                     metadata={"infra_error": infra_error, "claim": honesty.classify_claim(text)})

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = common.limited_rows(load_items(ctx), ctx)
    dataset = MemoryDataset([Sample(id=row["id"], input=row["prompt"], metadata={"item": row, "category": row.get("category")})
                             for row in rows])
    task = Task(dataset=dataset, solver=[harness.hermes_turn(ctx, SUITE, check_precondition=False)],
                scorer=out_of_band_check(ctx), name=SUITE)
    # The dataset is already trimmed to ctx.limit (stratified across categories, common.limited_rows):
    # no `limit=` here, or Inspect would re-truncate it back down to a first-N slice.
    log = common.run_task(task, ctx, model="none")
    return [to_item(sample, ctx) for sample in log.samples or []], []


def to_item(sample: Any, ctx: common.SuiteContext) -> dict[str, Any]:
    score = common.primary_score(sample)
    values = dict(score.value) if score else {}
    details = (score.metadata or {}) if score else {}
    turn = sample.metadata.get("turn") or {}
    context = sample.metadata.get("context") or {}
    item = common.sample_item(
        sample, ctx=ctx, suite=SUITE, subject=SUBJECT,
        scores={"artifact_ok": bool(values.get("artifact_ok")), "claimed_done": bool(values.get("claimed_done")),
                "check_error": bool(values.get("check_error")),
                "did_not_converge": bool(values.get("did_not_converge"))},
        metadata={"category": sample.metadata.get("category"), "check_detail": score.explanation if score else None,
                  "claim": details.get("claim"), "trajectory": sample.metadata.get("trajectory"),
                  "hermes_status": turn.get("status_code"), "hermes_error": turn.get("error"),
                  "hermes_error_kind": turn.get("error_kind"), "budget_exceeded": turn.get("budget_exceeded"),
                  "session_id": turn.get("session_id")},
        output=turn.get("text"),
        input_override=checks.build_prompt(sample.metadata["item"], context) if context else None)
    item["infra_error"] = bool(details.get("infra_error")) or common.model_error_item(sample)
    if turn.get("error") and not item["error"]:
        item["error"] = turn.get("error")
    return item
