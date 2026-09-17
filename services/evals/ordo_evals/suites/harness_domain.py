"""harness_domain (PRIVATE, judged): the `agent_standalone`-labelled slice of the private domain
candidate pool (E2b: private_dataset.py, `build-private` + `ingest-labels`; never in git) - asks a
judge labelled as needing a tool or the operator's own stored data, but complete instructions on
their own with no missing context. Each is sent through Hermes on a FRESH session (a unique session
id per item, same convention as harness_ops/harness_honesty - no shared conversation history) exactly
once; the reply is queued for the judge with the domain rubric plus one more criterion: did Hermes
actually use a tool to ground the answer, rather than answer from the model's own memory
(judge.AGENT_DOMAIN_CRITERIA). No programmatic score: this suite's metrics exist only after
`ingest-grades`. `self_contained` items go to model_domain instead; `conversation_dependent` items
are excluded from both (see private_dataset.label_counts, reported in this run's notes).
"""
from __future__ import annotations

import dataclasses
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import Generate, TaskState, solver

from ordo_evals import judge
from ordo_evals import private_dataset as pd
from ordo_evals.jsonl import read_jsonl
from ordo_evals.suites import common, harness

SUBJECT = "harness"
DESCRIPTION = ("Private agent_standalone operator asks (need a tool or the operator's own data), answered "
              "by Hermes on a fresh session, graded by the judge.")
SUITE = "harness_domain"


def _candidates_and_labels(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates_path = ctx.settings.results_dir / pd.CANDIDATES_FILE
    if not candidates_path.is_file():
        return [], {}
    candidates = read_jsonl(candidates_path)
    labels_path = ctx.settings.results_dir / pd.LABELS_FILE
    labels = pd.labels_by_id(read_jsonl(labels_path)) if labels_path.is_file() else {}
    return candidates, labels


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    if not ctx.settings.hermes_api_key:
        return "HERMES_API_SERVER_KEY is empty (the Hermes API server is not enabled)"
    candidates, labels = _candidates_and_labels(ctx)
    if not pd.items_with_label(candidates, labels, pd.LABEL_AGENT_STANDALONE):
        return ("no agent_standalone-labelled private candidates yet (run build-private, label "
                f"{pd.LABEL_QUEUE_FILE}, then ingest-labels)")
    return None


@solver
def hermes_fresh_turn(ctx: common.SuiteContext):
    """One Hermes turn per item on a brand-new session (harness.session_id_for is already unique
    per (run, suite, item)) - no honesty RESULT/FAILED protocol appended, unlike harness_ops/
    harness_honesty: this suite has no programmatic check, only a judge, so the reply should read as
    a normal answer to the operator's ask, not a formatted harness report."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        item_id = str(state.metadata["item_id"])
        session_id = harness.session_id_for(ctx.run_id, SUITE, item_id)
        turn = await ctx.hermes.chat(prompt=state.metadata["ask"], system=harness.EVAL_SYSTEM_PROMPT,
                                     session_id=session_id, session_key=harness.session_key_for(ctx.run_id),
                                     model=ctx.hermes_model_name)
        state.metadata["turn"] = dataclasses.asdict(turn)
        if turn.error_kind != "transport":
            traj = await harness.read_trajectory(ctx, turn.session_id)
            traj["wall_time_s"] = turn.wall_time_s
            state.metadata["trajectory"] = traj
        state.output = ModelOutput.from_content(model="hermes", content=turn.text or "")
        state.completed = True
        return state

    return solve


@scorer(metrics=[])
def queued_for_judge():
    async def score(state: TaskState, target: Target) -> Score:
        turn = state.metadata.get("turn") or {}
        if turn.get("error_kind") == "transport":
            return Score(value={"queued": 0}, answer=None, metadata={"infra_error": True})
        return Score(value={"queued": 1}, answer=turn.get("text"), metadata={"infra_error": False})

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, labels = _candidates_and_labels(ctx)
    rows = pd.items_with_label(candidates, labels, pd.LABEL_AGENT_STANDALONE)
    dataset = MemoryDataset([Sample(id=row["id"], input=row["input"],
                                    metadata={"item_id": row["id"], "ask": row["input"], "source": row.get("source")})
                             for row in rows])
    task = Task(dataset=dataset, solver=[hermes_fresh_turn(ctx)], scorer=queued_for_judge(), name=SUITE)
    # display="none": the prompts are private, keep them out of the container log (same rule as
    # model_domain - see its DESCRIPTION comment and the README's privacy section).
    log = common.run_task(task, ctx, model="none", display="none")
    items: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for sample in log.samples or []:
        turn = sample.metadata.get("turn") or {}
        item = common.sample_item(
            sample, ctx=ctx, suite=SUITE, subject=SUBJECT, scores={},
            metadata={"trajectory": sample.metadata.get("trajectory"), "hermes_status": turn.get("status_code"),
                      "hermes_error": turn.get("error"), "hermes_error_kind": turn.get("error_kind"),
                      "session_id": turn.get("session_id")},
            output=turn.get("text"), input_override=sample.metadata.get("ask"))
        item["infra_error"] = turn.get("error_kind") == "transport"
        items.append(item)
        if not item["infra_error"]:
            tools_used = sorted((sample.metadata.get("trajectory") or {}).get("tool_names") or [])
            queue.append(judge.queue_entry(
                run_id=ctx.run_id, suite=SUITE, item_id=item["item_id"], criteria=judge.AGENT_DOMAIN_CRITERIA,
                rubric=judge.AGENT_DOMAIN_RUBRIC, input_text=item["input"], output_text=item["output"] or "",
                context={"tools_used": tools_used}))
    # E2b: counts only, never content - see model_domain.run's matching note.
    ctx.notes.append(f"private domain labels: {pd.label_counts(candidates, labels)}")
    return items, queue
