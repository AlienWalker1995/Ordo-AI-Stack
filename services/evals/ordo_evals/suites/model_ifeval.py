"""model_ifeval: IFEval (inspect_evals/ifeval, dataset google/IFEval at the revision inspect_evals pins),
a FIXED sample of 60 prompts, strict and loose programmatic instruction checking.

The 60 are chosen by Inspect's seeded shuffle (`sample_shuffle=<seed>`) followed by `limit=60`, so a
given --seed always selects the same 60 prompts under the pinned inspect-ai, and `--limit N` (N < 60)
runs the first N of those same 60.
"""
from __future__ import annotations

from typing import Any

from inspect_evals.ifeval.ifeval import ifeval

from ordo_evals.suites import common

SUBJECT = "model"
DESCRIPTION = "IFEval, seeded sample of 60 prompts; strict and loose instruction-following accuracy."
SAMPLE_SIZE = 60


def unavailable_reason(ctx: common.SuiteContext) -> str | None:
    return None if ctx.settings.litellm_key else "LITELLM_KEY_EVALS is empty"


def run(ctx: common.SuiteContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    limit = SAMPLE_SIZE if ctx.limit is None else min(SAMPLE_SIZE, ctx.limit)
    log = common.run_task(ifeval(), ctx, model=common.model_spec(ctx.settings), limit=limit,
                          sample_shuffle=ctx.seed, **common.generate_args(ctx))
    items = []
    for sample in log.samples or []:
        score = common.primary_score(sample)
        values = dict(score.value) if score else {}
        items.append(common.sample_item(
            sample, ctx=ctx, suite="model_ifeval", subject=SUBJECT,
            scores={
                "prompt_level_strict": bool(values.get("prompt_level_strict")),
                "prompt_level_loose": bool(values.get("prompt_level_loose")),
                "inst_level_strict": int(values.get("inst_level_strict", 0)),
                "inst_level_loose": int(values.get("inst_level_loose", 0)),
                "num_instructions": int(values.get("num_instructions", 0)),
            },
            metadata={"instruction_id_list": sample.metadata.get("instruction_id_list"),
                      "stop_reason": sample.output.stop_reason if sample.output else None}))
        items[-1]["infra_error"] = common.model_error_item(sample)
    return items, []
