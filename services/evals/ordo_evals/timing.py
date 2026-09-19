"""Per-item throughput sanity signal (E15, round-6 fix): completion tokens per second, and a flag
for an item whose rate falls far below its suite's own median - visible even when the served model
name never changes (a saturated or thermal-throttled GPU is still slow, without any backend switch
to show it in `served_model`/`gpu_guard.py`).

Pure and suite-agnostic: `runner.run` calls `annotate_tokens_per_second` on each suite's items right
after they are collected (and redacted), before they are written to `items.jsonl`, so the flag is
part of the persisted record rather than a report-time recomputation.
"""
from __future__ import annotations

from typing import Any

# An item running at less than 1/10th its suite's median tokens/second is flagged `slow_item` -
# chosen the same way the model_reasoning/model_toolcall hard-tier bands were (README): by
# construction, not tuned to a specific incident, and expected to need retuning only if it turns out
# too noisy in practice (watch it the same way, don't re-derive it from scratch).
SLOW_ITEM_FACTOR = 10.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def contention(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-level contention block for summary.json (round-7 fix): the median and minimum per-item
    token rate across the WHOLE run and how many items were flagged `slow_item`, so a reader can see
    how loaded the box was without opening items.jsonl. No new collection - `annotate_tokens_per_second`
    above already recorded every rate; this only aggregates what is on the items.

    `n` is the number of items with a computable rate (never every item): a run whose suites produce no
    token counts at all reports n = 0 and null rates, not a fabricated 0.0 tokens/second."""
    rates = [rate for item in items
             if isinstance(rate := (item.get("metadata") or {}).get("tokens_per_second"), int | float)
             and not isinstance(rate, bool)]
    slow = sum(1 for item in items if (item.get("metadata") or {}).get("slow_item"))
    return {"n": len(rates), "slow_items": slow,
            "tokens_per_second_median": round(_median(rates), 2) if rates else None,
            "tokens_per_second_min": round(min(rates), 2) if rates else None}


def tokens_per_second(completion_tokens: Any, wall_time_s: Any) -> float | None:
    """None when either value is missing, non-numeric or non-positive - no ground truth to divide."""
    try:
        tokens = float(completion_tokens)
        seconds = float(wall_time_s)
    except (TypeError, ValueError):
        return None
    if tokens <= 0 or seconds <= 0:
        return None
    return tokens / seconds


def _item_tokens_per_second(item: dict[str, Any]) -> float | None:
    """Model suites: summed `output_tokens` across `item["usage"]` (Inspect's own per-model usage
    record, keyed by model id - see `data/evals/runs/*/items.jsonl` for the shape) over
    `item["time_s"]` (the sample's total wall time). Harness suites: `usage` is always empty
    (Inspect never calls a model provider directly for them - Hermes does), so fall back to the
    trajectory's `completion_tokens` / `wall_time_s` (`hermes_turn.call_hermes`)."""
    usage = item.get("usage") or {}
    completion = sum(int(u.get("output_tokens") or 0) for u in usage.values()) if usage else None
    time_s = item.get("time_s")
    if not completion or not time_s:
        trajectory = (item.get("metadata") or {}).get("trajectory") or {}
        completion = completion or trajectory.get("completion_tokens")
        time_s = time_s or trajectory.get("wall_time_s")
    return tokens_per_second(completion, time_s)


def annotate_tokens_per_second(items: list[dict[str, Any]]) -> None:
    """Mutates `items` in place: `metadata.tokens_per_second` on every item where computable, and
    `metadata.slow_item = True` on one whose rate is more than SLOW_ITEM_FACTOR below the suite's
    own median (of the items with a computable rate). Fewer than 2 computable rates means there is no
    meaningful median to compare against, so nothing is flagged (tokens_per_second is still recorded
    when there's exactly one)."""
    rates: list[float] = []
    for item in items:
        tps = _item_tokens_per_second(item)
        if tps is not None:
            if item.get("metadata") is None:
                item["metadata"] = {}
            item["metadata"]["tokens_per_second"] = round(tps, 2)
            rates.append(tps)
    if len(rates) < 2:
        return
    threshold = _median(rates) / SLOW_ITEM_FACTOR
    for item in items:
        tps = (item.get("metadata") or {}).get("tokens_per_second")
        if tps is not None and tps < threshold:
            item["metadata"]["slow_item"] = True
