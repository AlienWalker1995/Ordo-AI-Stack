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


# ── contention (round-7 fix) ──────────────────────────────────────────────────────

def test_contention_reports_the_runs_median_and_minimum_rate_and_the_slow_count():
    """The run-level block summary.json carries so a reader can see how loaded the box was without
    opening items.jsonl. Nothing new is collected: it aggregates the rates already on the items."""
    items = [model_item(100, 10.0, "a"), model_item(100, 10.0, "b"), model_item(100, 10.0, "c"),
            model_item(20, 100.0, "cpu-fallback")]
    timing.annotate_tokens_per_second(items)
    block = timing.contention(items)
    assert block == {"n": 4, "slow_items": 1, "tokens_per_second_median": 10.0,
                     "tokens_per_second_min": 0.2, "abandoned_items": 0, "abandoned_overrun_s": 0.0,
                     "abandoned_wait_timeouts": 0}


def test_contention_over_items_with_no_computable_rate_is_null_not_zero():
    items = [model_item(0, 10.0, "no-tokens")]
    timing.annotate_tokens_per_second(items)
    assert timing.contention(items) == {"n": 0, "slow_items": 0, "tokens_per_second_median": None,
                                        "tokens_per_second_min": None, "abandoned_items": 0,
                                        "abandoned_overrun_s": 0.0, "abandoned_wait_timeouts": 0}


def test_items_within_an_order_of_magnitude_of_the_median_are_not_flagged():
    """Normal run-to-run variance (a longer prompt, a bit of thermal noise) must not trip the flag -
    only a change closer to the fallback's actual order-of-magnitude slowdown should."""
    items = [model_item(100, 10.0, "a"), model_item(100, 12.0, "b"), model_item(100, 8.0, "c")]
    timing.annotate_tokens_per_second(items)
    assert all(not i["metadata"].get("slow_item", False) for i in items)


# ── E23 (round-10 fix): the run's abandoned work ─────────────────────────────────

def abandoned_item(overrun_s, item_id="h", timed_out=False):
    """A harness item whose budget fired: `hermes_turn.call_hermes` waited `overrun_s` for Hermes to
    release the model slot before the next item started."""
    item = harness_item(100, 900.0, item_id)
    item["metadata"]["trajectory"].update({"overrun_s": overrun_s, "overrun_timed_out": timed_out})
    return item


def test_contention_totals_the_seconds_the_run_spent_on_abandoned_items():
    """The loop5-20260919-1600 shape: an item exceeds its 900s budget and Hermes keeps working it for
    another ~14 minutes on the single model slot. The run-level total is what tells a reader how much
    of the run was spent that way."""
    items = [harness_item(100, 10.0, "converged"), abandoned_item(873.4, "hon-07"),
             abandoned_item(852.6, "pd-1")]
    timing.annotate_tokens_per_second(items)
    block = timing.contention(items)
    assert block["abandoned_items"] == 2
    assert block["abandoned_overrun_s"] == 1726.0
    assert block["abandoned_wait_timeouts"] == 0


def test_contention_counts_a_wait_that_timed_out_separately():
    """A wait that hit its own bound means the next item DID start while the abandoned one was still
    generating - the one case where the harness could not remove the contention it created."""
    block = timing.contention([abandoned_item(900.0, "hon-07", timed_out=True)])
    assert block["abandoned_items"] == 1 and block["abandoned_wait_timeouts"] == 1


def test_items_from_a_run_that_predates_the_overrun_measurement_total_zero():
    """A recorded run with no `overrun_s` anywhere reports 0.0, not a fabricated number - the same
    rule the token rates follow."""
    block = timing.contention([harness_item(100, 10.0, "old")])
    assert block["abandoned_items"] == 0 and block["abandoned_overrun_s"] == 0.0
