"""Suite registry (pure: importing this does not import the eval framework).

A suite module (loaded by `load`) exposes:
    SUBJECT                       "model" or "harness"
    DESCRIPTION                   one line, used as the Langfuse dataset description
    unavailable_reason(ctx)       None, or why the suite cannot run on this stack right now
    run(ctx)                      run the Inspect task; returns (items, judge_queue_entries)
"""
from __future__ import annotations

import importlib
from types import ModuleType

# Run order: cheap model suites first, then the judged model suite, then Hermes (the slowest).
SUITE_ORDER = ("model_ifeval", "model_toolcall", "model_reasoning", "model_domain", "harness_ops", "harness_honesty")
SUBJECTS = {
    "model_ifeval": "model",
    "model_toolcall": "model",
    "model_reasoning": "model",
    "model_domain": "model",
    "harness_ops": "harness",
    "harness_honesty": "harness",
}


def resolve_suites(spec: str) -> list[str]:
    """`all` or a comma-separated list, returned in run order. Unknown names raise ValueError."""
    if spec.strip() == "all":
        return list(SUITE_ORDER)
    requested = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = sorted(set(requested) - set(SUITE_ORDER))
    if unknown:
        raise ValueError(f"unknown suite(s) {unknown}; choose from {list(SUITE_ORDER)} or 'all'")
    if not requested:
        raise ValueError("no suites requested")
    return [s for s in SUITE_ORDER if s in requested]


def load(name: str) -> ModuleType:
    if name not in SUBJECTS:
        raise ValueError(f"unknown suite {name!r}")
    return importlib.import_module(f"ordo_evals.suites.{name}")
