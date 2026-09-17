"""E5 (fix-round-1 brief): `--limit N` must be a seeded, category-stratified sample, not the first N
items. ordo_evals.sampling is pure (no inspect_ai import) so it is exercised directly here."""
from __future__ import annotations

from ordo_evals.sampling import filter_rows_to_ids, sampled_item_ids, stratified_limit_ids


def rows(*category_counts: tuple[str, int]) -> list[dict]:
    """[{"id": "<category>-<n>", "category": category}, ...] for each (category, count) pair, in the
    same shape as arithmetic-01/units-01/... in reasoning.jsonl or single_call-01/... in toolcall.jsonl."""
    made = []
    for category, count in category_counts:
        for i in range(1, count + 1):
            made.append({"id": f"{category}-{i:02d}", "category": category})
    return made


REASONING_LIKE = rows(("arithmetic", 10), ("units", 10), ("dates", 10), ("logic", 10))
TOOLCALL_LIKE = rows(("single_call", 7), ("parallel", 7), ("multi_turn", 6), ("no_tool", 7),
                     ("arg_types", 7), ("enum", 6))


def test_no_limit_or_a_limit_at_or_above_the_row_count_returns_everything():
    assert stratified_limit_ids(REASONING_LIKE, None, seed=1) is None
    assert stratified_limit_ids(REASONING_LIKE, len(REASONING_LIKE), seed=1) is None
    assert stratified_limit_ids(REASONING_LIKE, len(REASONING_LIKE) + 5, seed=1) is None
    assert filter_rows_to_ids(REASONING_LIKE, None) == REASONING_LIKE


def test_a_small_limit_spans_every_category_instead_of_taking_the_first_n():
    """The bug this fixes: --limit 6 on reasoning.jsonl used to be arithmetic-01..06 only."""
    ids = stratified_limit_ids(REASONING_LIKE, 6, seed=1234)
    assert len(ids) == 6
    categories = {item_id.rsplit("-", 1)[0] for item_id in ids}
    assert categories == {"arithmetic", "units", "dates", "logic"}, (
        "a stratified sample of 6 across 4 categories must touch every category")


def test_a_limit_smaller_than_the_category_count_still_covers_as_many_as_it_can():
    ids = stratified_limit_ids(TOOLCALL_LIKE, 6, seed=1234)
    assert len(ids) == 6
    categories = {item_id.rsplit("-", 1)[0] for item_id in ids}
    assert len(categories) == 6, "6 categories, limit 6: one id from each"


def test_deterministic_for_a_given_seed_and_varies_across_seeds():
    first = stratified_limit_ids(REASONING_LIKE, 8, seed=1234)
    again = stratified_limit_ids(REASONING_LIKE, 8, seed=1234)
    other_seed = stratified_limit_ids(REASONING_LIKE, 8, seed=99)
    assert first == again
    assert first != other_seed  # different seed, different specific ids (still spans all categories)
    assert {i.rsplit("-", 1)[0] for i in other_seed} == {"arithmetic", "units", "dates", "logic"}


def test_every_round_robin_slot_is_used_before_a_category_repeats():
    """limit 5 across 4 categories: round 1 picks one from each (4 ids), round 2 picks a 5th from
    whichever category still has ids left (alphabetically first: arithmetic)."""
    ids = stratified_limit_ids(REASONING_LIKE, 5, seed=1)
    counts: dict[str, int] = {}
    for item_id in ids:
        category = item_id.rsplit("-", 1)[0]
        counts[category] = counts.get(category, 0) + 1
    assert sorted(counts.values()) == [1, 1, 1, 2]


def test_filter_rows_to_ids_preserves_the_given_id_order_and_drops_the_rest():
    ids = ["units-03", "arithmetic-01"]
    filtered = filter_rows_to_ids(REASONING_LIKE, ids)
    assert [row["id"] for row in filtered] == ids


def test_a_missing_category_becomes_its_own_group_rather_than_erroring():
    mixed = [{"id": "a"}, {"id": "b"}, {"id": "c", "category": "x"}, {"id": "d", "category": "x"}]
    ids = stratified_limit_ids(mixed, 2, seed=1)
    assert len(ids) == 2


def test_sampled_item_ids_is_none_without_a_limit_and_sorted_deduplicated_with_one():
    items = [{"item_id": "b"}, {"item_id": "a"}, {"item_id": "a"}]
    assert sampled_item_ids(items, None) is None
    assert sampled_item_ids(items, 5) == ["a", "b"]
