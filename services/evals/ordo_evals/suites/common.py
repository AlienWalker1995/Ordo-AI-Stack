"""Shared plumbing for the Inspect-based suites: the run context, the model spec, running a task,
and turning an Inspect sample into an items.jsonl record."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import ChatMessage

from ordo_evals.ids import trace_id_for
from ordo_evals.sampling import filter_rows_to_ids, stratified_limit_ids
from ordo_evals.settings import Settings

# Inspect's generic OpenAI-compatible provider: `openai-api/<service>/<model>` reads
# <SERVICE>_BASE_URL and <SERVICE>_API_KEY from the environment. The key is passed through the
# environment, never through model_args, because Inspect records model_args in every .eval log.
MODEL_SERVICE = "ordo"


@dataclasses.dataclass
class SuiteContext:
    run_id: str
    seed: int
    limit: int | None
    settings: Settings
    run_dir: Path
    probes: Any = None          # probes.LiveProbes (harness suites)
    hermes: Any = None          # hermes_client.HermesClient (harness suites)
    hermes_model_name: str = ""  # the model id Hermes's API server advertises
    # E15: the run's declared active GPU model (runner._served_model), threaded through so the
    # harness suites can classify their own per-item served backend against it
    # (gpu_guard.served_model_for_item, called from hermes_turn.call_hermes).
    served_model: str = ""
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def inspect_log_dir(self) -> str:
        return str(self.run_dir / "inspect")


def model_spec(settings: Settings) -> str:
    os.environ[f"{MODEL_SERVICE.upper()}_BASE_URL"] = settings.model_base_url
    os.environ[f"{MODEL_SERVICE.upper()}_API_KEY"] = settings.litellm_key
    return f"openai-api/{MODEL_SERVICE}/{settings.model_name}"


def generate_args(ctx: SuiteContext) -> dict[str, Any]:
    """Generation settings shared by the model suites: the deployment's own sampling defaults,
    a fixed seed, and one request at a time (llama.cpp serves a single slot that Hermes crons share)."""
    args: dict[str, Any] = {"seed": ctx.seed, "max_connections": 1}
    if ctx.settings.model_max_tokens:
        args["max_tokens"] = ctx.settings.model_max_tokens
    return args


def limited_rows(rows: list[dict[str, Any]], ctx: SuiteContext,
                 category_key: str = "category") -> list[dict[str, Any]]:
    """`rows` restricted to `ctx.limit`, spread across `category_key` (ordo_evals.sampling), so a
    smoke run exercises more than one category (fix-round-1 brief, E5). Unchanged when `ctx.limit` is
    None or not smaller than `len(rows)`."""
    ids = stratified_limit_ids(rows, ctx.limit, ctx.seed, category_key=category_key)
    return filter_rows_to_ids(rows, ids)


def run_task(task: Task, ctx: SuiteContext, *, model: str, limit: int | None = None,
             sample_shuffle: int | None = None, display: str = "plain", **kwargs: Any) -> EvalLog:
    """Run one task at concurrency 1 and return its log. Sample errors do not abort the run: they
    are recorded on the sample and surface as items with `error` set."""
    logs = inspect_eval(
        task, model=model, log_dir=ctx.inspect_log_dir, display=display, limit=limit,
        sample_shuffle=sample_shuffle, max_samples=1, max_tasks=1, fail_on_error=False,
        log_level="warning", **kwargs)
    return logs[0]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            text = getattr(part, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def input_text(sample: EvalSample) -> str:
    """The sample input as text: the last user message for chat inputs."""
    if isinstance(sample.input, str):
        return sample.input
    users = [m for m in sample.input if getattr(m, "role", "") == "user"]
    chosen: ChatMessage | None = users[-1] if users else (sample.input[-1] if sample.input else None)
    return message_text(chosen.content) if chosen is not None else ""


def sample_item(sample: EvalSample, *, ctx: SuiteContext, suite: str, subject: str,
                scores: dict[str, Any], metadata: dict[str, Any], output: str | None = None,
                input_override: str | None = None, target: Any = None,
                served_model: str | None = None) -> dict[str, Any]:
    """`served_model` (E15): which deployment served THIS item. Model suites need no override - the
    default below reads `sample.output.model`, the raw completion response's own `model` field
    (verified authoritative for the local backend, see gpu_guard.py's module docstring), which
    Inspect already carries with no extra call. Harness suites have no such field on their synthetic
    `ModelOutput.from_content(model="hermes", ...)` (see suites/harness.py), so they pass the value
    `hermes_turn.call_hermes` already computed via `gpu_guard.served_model_for_item`."""
    item_id = str(sample.id)
    error = sample.error.message if sample.error else None
    if served_model is None and sample.output is not None:
        served_model = sample.output.model
    return {
        "run_id": ctx.run_id,
        "suite": suite,
        "subject": subject,
        "item_id": item_id,
        "input": input_override if input_override is not None else input_text(sample),
        "output": output if output is not None else (sample.output.completion if sample.output else None),
        "target": target,
        "served_model": served_model,
        "scores": scores,
        "metadata": metadata,
        "trace_id": trace_id_for(ctx.run_id, suite, item_id),
        "error": error,
        "infra_error": False,
        "usage": {name: usage.model_dump(exclude_none=True) for name, usage in (sample.model_usage or {}).items()},
        "time_s": sample.total_time,
    }


def primary_score(sample: EvalSample) -> Any:
    """The single scorer's Score for a sample (every suite task declares exactly one scorer)."""
    if not sample.scores:
        return None
    return next(iter(sample.scores.values()))


def model_error_item(sample: EvalSample) -> bool:
    """True when the sample failed before scoring because the MODEL ENDPOINT failed (an infra error),
    rather than the model answering badly."""
    return bool(sample.error) and not sample.scores
