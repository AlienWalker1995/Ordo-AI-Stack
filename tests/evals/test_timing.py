"""E15 (round-6 fix): the per-item throughput sanity signal (ordo_evals.timing) - pure, no inspect_ai
import, exercised against items.jsonl-shaped dicts (see data/evals/runs/*/items.jsonl for the real
`usage` and `trajectory` shapes this reads)."""
from __future__ import annotations

from ordo_evals import timing


def model_item(output_tokens, time_s, item_id="a"):
    return {"item_id": item_id, "usage": {"openai-api/ordo/local-chat": {"output_tokens": output_tokens}},
           "time_s": time_s, "metadata": {}}


def harness_item(completion_tokens, wall_time_s, item_id="h"):
    return {"item_id": item_id, "usage": {}, "time_s": None,
           "metadata": {"trajectory": {"completion_tokens": completion_tokens, "wall_time_s": wall_time_s}}}


# ── tokens_per_second ────────────────────────────────────────────────────────────

def test_tokens_per_second_divides_tokens_by_seconds():
    assert timing.tokens_per_second(100, 10) == 10.0


def test_tokens_per_second_is_none_for_missing_or_non_positive_values():
    assert timing.tokens_per_second(None, 10) is None
    assert timing.tokens_per_second(100, None) is None
    assert timing.tokens_per_second(0, 10) is None
    assert timing.tokens_per_second(100, 0) is None
    assert timing.tokens_per_second("not-a-number", 10) is None


# ── annotate_tokens_per_second ────────────────────────────────────────────────────

def test_model_suite_items_use_usage_output_tokens_over_time_s():
    items = [model_item(100, 10.0)]
    timing.annotate_tokens_per_second(items)
    assert items[0]["metadata"]["tokens_per_second"] == 10.0


def test_harness_suite_items_fall_back_to_trajectory_fields():
    items = [harness_item(150, 30.0)]
    timing.annotate_tokens_per_second(items)
    assert items[0]["metadata"]["tokens_per_second"] == 5.0


def test_an_item_with_no_computable_rate_gets_no_field_and_is_never_flagged():
    items = [model_item(0, 10.0, "no-tokens"), model_item(50, 5.0, "normal")]
    timing.annotate_tokens_per_second(items)
    assert "tokens_per_second" not in items[0]["metadata"]
    assert "slow_item" not in items[0]["metadata"]
    assert items[1]["metadata"]["tokens_per_second"] == 10.0


def test_fewer_than_two_computable_rates_flags_nothing():
    """No meaningful median with only one data point - the rate is still recorded, just not judged."""
    items = [model_item(100, 10.0)]
    timing.annotate_tokens_per_second(items)
    assert items[0]["metadata"]["tokens_per_second"] == 10.0
    assert "slow_item" not in items[0]["metadata"]


def test_an_item_an_order_of_magnitude_below_the_median_is_flagged_slow():
    """The loop4-shaped failure this exists to catch: most items serve normally, one (or a few) run
    on the slow CPU fallback and take far longer per token for the same amount of work."""
    items = [model_item(100, 10.0, "a"), model_item(100, 10.0, "b"), model_item(100, 10.0, "c"),
            model_item(20, 100.0, "cpu-fallback")]  # 10 tok/s normal x3, 0.2 tok/s here
    timing.annotate_tokens_per_second(items)
    slow = {i["item_id"]: i["metadata"].get("slow_item", False) for i in items}
    assert slow == {"a": False, "b": False, "c": False, "cpu-fallback": True}


def test_items_within_an_order_of_magnitude_of_the_median_are_not_flagged():
    """Normal run-to-run variance (a longer prompt, a bit of thermal noise) must not trip the flag -
    only a change closer to the fallback's actual order-of-magnitude slowdown should."""
    items = [model_item(100, 10.0, "a"), model_item(100, 12.0, "b"), model_item(100, 8.0, "c")]
    timing.annotate_tokens_per_second(items)
    assert all(not i["metadata"].get("slow_item", False) for i in items)
