"""model_reasoning: 40 short-answer items (arithmetic, units, dates, logic), exact match after
normalization (see normalize.py). Dataset: datasets/reasoning.jsonl (generic, committed)."""
from __future__ import annotations

from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ChatMessageSystem, ChatMessageUser
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState, generate

from ordo_evals.jsonl import read_jsonl
from ordo_evals.normalize import answer_for_scoring, answers_match, extract_final_answer
from ordo_evals.suites import common

SUBJECT = "model"
DESCRIPTION = "Short-answer reasoning (arithmetic, unit conversion, dates, logic); exact match after normalization."
SYSTEM_PROMPT = ("Solve the problem. You may reason briefly. End your reply with a final line of the form "
                 "`ANSWER: <answer>` that contains only the answer in the requested format.")


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    return None if ctx.settings.litellm_key else "LITELLM_KEY_EVALS is empty"


def _dataset(ctx: common.SuiteContext) -> MemoryDataset:
    rows = common.limited_rows(list(read_jsonl(ctx.settings.datasets_dir / "reasoning.jsonl")), ctx)
    samples = []
    for row in rows:
        samples.append(Sample(
            id=row["id"], target=row["answer"],
            input=[ChatMessageSystem(content=SYSTEM_PROMPT), ChatMessageUser(content=row["question"])],
            metadata={"category": row["category"], "aliases": row.get("aliases", []),
                      "tolerance": row.get("tolerance", 0.0)}))
    return MemoryDataset(samples)


@scorer(metrics={"correct": [mean()], "format_ok": [mean()]})
def exact_answer():
    async def score(state: TaskState, target: Target) -> Score:
        completion = state.output.completion
        answer = answer_for_scoring(completion)
        correct = answers_match(target.text, answer, aliases=state.metadata.get("aliases"),
                                tolerance=float(state.metadata.get("tolerance", 0.0)))
        format_ok = extract_final_answer(completion) is not None
        return Score(value={"correct": int(correct), "format_ok": int(format_ok)}, answer=answer)

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    task = Task(dataset=_dataset(ctx), solver=[generate()], scorer=exact_answer(), name="model_reasoning")
    # The dataset is already trimmed to ctx.limit (stratified across categories, common.limited_rows):
    # no `limit=` here, or Inspect would re-truncate it back down to a first-N slice.
    log = common.run_task(task, ctx, model=common.model_spec(ctx.settings), **common.generate_args(ctx))
    items = []
    for sample in log.samples or []:
        score = common.primary_score(sample)
        values = dict(score.value) if score else {}
        items.append(common.sample_item(
            sample, ctx=ctx, suite="model_reasoning", subject=SUBJECT,
            scores={"correct": bool(values.get("correct")), "format_ok": bool(values.get("format_ok"))},
            metadata={"category": sample.metadata.get("category"), "extracted_answer": score.answer if score else None,
                      "stop_reason": sample.output.stop_reason if sample.output else None},
            target=sample.target))
        items[-1]["infra_error"] = common.model_error_item(sample)
    return items, []
