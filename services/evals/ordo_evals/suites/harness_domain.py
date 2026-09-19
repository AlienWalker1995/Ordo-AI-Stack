"""harness_domain (PRIVATE, judged): the `agent_standalone`-AND-`read_only`-labelled slice of the
private domain candidate pool (E2b: private_dataset.py, `build-private` + `ingest-labels`; never in
git) - asks a judge labelled as needing a tool or the operator's own stored data, complete
instructions on their own with no missing context, AND labelled as answerable by inspecting state
only, never by changing it (E11 - see judge.PRIVATE_LABEL_RUBRIC's "mutation" criterion). Each is sent
through Hermes on a FRESH session (a unique session id per item, same convention as
harness_ops/harness_honesty - no shared conversation history) exactly once, with a standing
read-only instruction layered onto the prompt (ordo_evals.prompts.DOMAIN_SYSTEM_PROMPT); the reply is
queued for the judge with the domain rubric plus one more criterion: did Hermes actually use a tool to
ground the answer, rather than answer from the model's own memory (judge.AGENT_DOMAIN_CRITERIA). No
programmatic score: this suite's metrics exist only after `ingest-grades`. `self_contained` items go
to model_domain instead; `conversation_dependent` items, and any agent_standalone item labelled
mutating (or not yet mutation-labelled), are excluded from this suite (see private_dataset.label_counts
and private_dataset.mutation_counts, both reported in this run's notes so every exclusion stays
visible).

E11 (safety): iteration 2 sent an agent_standalone item ("add hackernews to the ai-daily-news site")
through Hermes, which has full tools, the Docker socket and real repo access - Hermes cloned a real
repo, edited it, committed and attempted to push; the push only failed because the GitHub tokens were
expired. An eval item must never be able to mutate a real system by luck. Two independent layers:
  1. the mutation label excludes any candidate a judge marked (or left unmarked) mutating from ever
     reaching this suite at all - see run() below, which builds the Inspect dataset directly from
     `pd.items_with_label_and_mutation(..., label=LABEL_AGENT_STANDALONE, mutation=MUTATION_READ_ONLY)`;
  2. DOMAIN_SYSTEM_PROMPT tells Hermes, for every item that DOES reach it, to answer by inspecting only
     and make no changes. This is defence in depth, not a guarantee: Hermes can still choose to act
     against an instruction, the same way any agent can (see the README's harness_domain section).
"""
from __future__ import annotations

import dataclasses
from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import Generate, TaskState, solver

from ordo_evals import hermes_turn, judge
from ordo_evals import private_dataset as pd
from ordo_evals.jsonl import read_jsonl
from ordo_evals.prompts import DOMAIN_SYSTEM_PROMPT
from ordo_evals.suites import common, harness

SUBJECT = "harness"
DESCRIPTION = ("Private agent_standalone, read_only operator asks (need a tool or the operator's own data, "
              "answerable by looking something up rather than changing anything), answered by Hermes on "
              "a fresh session, graded by the judge.")
SUITE = "harness_domain"


def _candidates_and_labels(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    candidates_path = ctx.settings.results_dir / pd.CANDIDATES_FILE
    if not candidates_path.is_file():
        return [], {}, {}
    candidates = read_jsonl(candidates_path)
    labels_path = ctx.settings.results_dir / pd.LABELS_FILE
    label_rows = read_jsonl(labels_path) if labels_path.is_file() else []
    return candidates, pd.labels_by_id(label_rows), pd.mutations_by_id(label_rows)


def _read_only_agent_standalone(candidates: list[dict[str, Any]], labels: dict[str, str],
                                mutations: dict[str, str]) -> list[dict[str, Any]]:
    return pd.items_with_label_and_mutation(candidates, labels, mutations,
                                            label=pd.LABEL_AGENT_STANDALONE, mutation=pd.MUTATION_READ_ONLY)


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    if not ctx.settings.hermes_api_key:
        return "HERMES_API_SERVER_KEY is empty (the Hermes API server is not enabled)"
    candidates, labels, mutations = _candidates_and_labels(ctx)
    if not _read_only_agent_standalone(candidates, labels, mutations):
        return ("no agent_standalone-AND-read_only-labelled private candidates yet (run build-private, "
                f"label {pd.LABEL_QUEUE_FILE} on both the label and mutation criteria, then ingest-labels)")
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
        # E10: call_hermes applies the per-item wall-clock budget and, on any timeout, recovers the
        # trajectory and last assistant message from state.db so the item is a real result
        # (did_not_converge) rather than a discarded infra_error - see harness.call_hermes.
        turn, traj = await harness.call_hermes(ctx, prompt=state.metadata["ask"], system=DOMAIN_SYSTEM_PROMPT,
                                               session_id=session_id, session_key=harness.session_key_for(ctx.run_id),
                                               model=ctx.hermes_model_name)
        state.metadata["turn"] = dataclasses.asdict(turn)
        if traj:
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
            return Score(value={"queued": 0, "did_not_converge": 0}, answer=None, metadata={"infra_error": True})
        # E10: a timeout with a recovered trajectory is a real result (did_not_converge), not an infra
        # error; a timeout where state.db has nothing at all (the session never even started) has
        # nothing to grade, so it is still excluded like a transport failure.
        did_not_converge = turn.get("error_kind") == "timeout"
        found = bool((state.metadata.get("trajectory") or {}).get("found"))
        infra_error = did_not_converge and not found
        return Score(value={"queued": 0 if infra_error else 1, "did_not_converge": int(did_not_converge)},
                     answer=turn.get("text"), metadata={"infra_error": infra_error})

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, labels, mutations = _candidates_and_labels(ctx)
    # E11: only agent_standalone AND read_only candidates ever become a Sample here - a mutating (or
    # mutation-unlabelled) item never reaches this dataset, so it can never reach Hermes through this
    # suite no matter what happens downstream.
    rows = _read_only_agent_standalone(candidates, labels, mutations)
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
        score = common.primary_score(sample)
        values = dict(score.value) if score else {}
        details = (score.metadata or {}) if score else {}
        item = common.sample_item(
            sample, ctx=ctx, suite=SUITE, subject=SUBJECT,
            scores={"did_not_converge": bool(values.get("did_not_converge"))},
            metadata={"trajectory": sample.metadata.get("trajectory"), "hermes_status": turn.get("status_code"),
                      "hermes_error": turn.get("error"), "hermes_error_kind": turn.get("error_kind"),
                      "budget_exceeded": turn.get("budget_exceeded"), "session_id": turn.get("session_id")},
            output=turn.get("text"), input_override=sample.metadata.get("ask"),
            served_model=(sample.metadata.get("trajectory") or {}).get("served_model"))
        item["infra_error"] = bool(details.get("infra_error")) or common.model_error_item(sample)
        items.append(item)
        # E18 (round-7 fix): a NON-CONVERGED item never reaches the judge, whether or not its
        # state.db recovery happened to catch a fragment of text - it is a non-convergence, counted by
        # did_not_converge_rate, not a judged failure (hermes_turn.judgeable).
        if hermes_turn.judgeable(infra_error=item["infra_error"],
                                 did_not_converge=item["scores"]["did_not_converge"], text=item["output"]):
            tools_used = sorted((sample.metadata.get("trajectory") or {}).get("tool_names") or [])
            queue.append(judge.queue_entry(
                run_id=ctx.run_id, suite=SUITE, item_id=item["item_id"], criteria=judge.AGENT_DOMAIN_CRITERIA,
                rubric=judge.AGENT_DOMAIN_RUBRIC, input_text=item["input"], output_text=item["output"] or "",
                context={"tools_used": tools_used}))
    # E2b: counts only, never content - see model_domain.run's matching note.
    ctx.notes.append(f"private domain labels: {pd.label_counts(candidates, labels)}")
    # E11: counts only - makes the mutating (and not-yet-mutation-labelled) exclusion visible among
    # the agent_standalone candidates, exactly like the conversation_dependent exclusion above.
    ctx.notes.append(f"private domain agent_standalone mutation labels: "
                     f"{pd.mutation_counts(candidates, labels, mutations, pd.LABEL_AGENT_STANDALONE)}")
    return items, queue
