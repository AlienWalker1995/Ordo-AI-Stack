"""model_domain (PRIVATE, judged): real operator asks sampled by `build-private` from Hermes's state.db
into /results/datasets/private_domain.jsonl (never in git). The model answers each ask directly; every
answer is queued for the judge with the domain rubric (judge.DOMAIN_CRITERIA). No programmatic score:
this suite's metrics exist only after `ingest-grades`."""
from __future__ import annotations

from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import TaskState, generate

from ordo_evals import judge
from ordo_evals.jsonl import read_jsonl
from ordo_evals.suites import common

SUBJECT = "model"
DESCRIPTION = "Private operator asks (built from Hermes state.db), answered by the bare model, graded by the judge."
DATASET_FILE = "datasets/private_domain.jsonl"


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    if not ctx.settings.litellm_key:
        return "LITELLM_KEY_EVALS is empty"
    if not (ctx.settings.results_dir / DATASET_FILE).is_file():
        return f"{DATASET_FILE} not built yet (run `build-private` first)"
    return None


@scorer(metrics=[])
def queued_for_judge():
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value={"queued": 1}, answer=None)

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = read_jsonl(ctx.settings.results_dir / DATASET_FILE)
    dataset = MemoryDataset([Sample(id=row["id"], input=row["input"], metadata={"source": row.get("source")})
                             for row in rows])
    task = Task(dataset=dataset, solver=[generate()], scorer=queued_for_judge(), name="model_domain")
    # display="none": the prompts are private, keep them out of the container log.
    log = common.run_task(task, ctx, model=common.model_spec(ctx.settings), limit=ctx.limit, display="none",
                          **common.generate_args(ctx))
    items: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for sample in log.samples or []:
        item = common.sample_item(sample, ctx=ctx, suite="model_domain", subject=SUBJECT, scores={},
                                  metadata={"stop_reason": sample.output.stop_reason if sample.output else None})
        item["infra_error"] = common.model_error_item(sample)
        items.append(item)
        if not item["infra_error"]:
            queue.append(judge.queue_entry(
                run_id=ctx.run_id, suite="model_domain", item_id=item["item_id"], criteria=judge.DOMAIN_CRITERIA,
                rubric=judge.DOMAIN_RUBRIC, input_text=item["input"], output_text=item["output"] or ""))
    return items, queue
