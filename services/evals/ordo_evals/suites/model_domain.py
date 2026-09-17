"""model_domain (PRIVATE, judged): the `self_contained`-labelled slice of the private domain
candidate pool (E2b: private_dataset.py, `build-private` + `ingest-labels`; never in git). Only asks
a judge has labelled answerable by a bare model with no tools and no conversation history are used
here - the `agent_standalone` slice goes through Hermes instead (harness_domain), and
`conversation_dependent` items are excluded from both (see private_dataset.label_counts, reported in
this run's notes so the exclusion stays visible). The model answers each self_contained ask directly;
every answer is queued for the judge with the domain rubric (judge.DOMAIN_CRITERIA). No programmatic
score: this suite's metrics exist only after `ingest-grades`."""
from __future__ import annotations

from typing import Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import TaskState, generate

from ordo_evals import judge
from ordo_evals import private_dataset as pd
from ordo_evals.jsonl import read_jsonl
from ordo_evals.suites import common

SUBJECT = "model"
DESCRIPTION = ("Private self_contained operator asks (judge-labelled from the private candidate pool), "
              "answered by the bare model, graded by the judge.")


def _candidates_and_labels(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates_path = ctx.settings.results_dir / pd.CANDIDATES_FILE
    if not candidates_path.is_file():
        return [], {}
    candidates = read_jsonl(candidates_path)
    labels_path = ctx.settings.results_dir / pd.LABELS_FILE
    labels = pd.labels_by_id(read_jsonl(labels_path)) if labels_path.is_file() else {}
    return candidates, labels


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    if not ctx.settings.litellm_key:
        return "LITELLM_KEY_EVALS is empty"
    candidates, labels = _candidates_and_labels(ctx)
    if not pd.items_with_label(candidates, labels, pd.LABEL_SELF_CONTAINED):
        return ("no self_contained-labelled private candidates yet (run build-private, label "
                f"{pd.LABEL_QUEUE_FILE}, then ingest-labels)")
    return None


@scorer(metrics=[])
def queued_for_judge():
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value={"queued": 1}, answer=None)

    return score


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, labels = _candidates_and_labels(ctx)
    rows = pd.items_with_label(candidates, labels, pd.LABEL_SELF_CONTAINED)
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
    # E2b: counts only, never content - makes the conversation_dependent (and still-unlabeled)
    # exclusion visible in summary.json without printing or committing any operator text.
    ctx.notes.append(f"private domain labels: {pd.label_counts(candidates, labels)}")
    return items, queue
