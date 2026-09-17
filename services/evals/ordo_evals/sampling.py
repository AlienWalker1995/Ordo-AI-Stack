"""`--limit N`: a seeded, category-stratified sample of dataset rows, not the first N.

Every model/harness dataset row carries a `category`. Before this module existed, `--limit N` was
handed straight to Inspect as `limit=`, which truncates the (unshuffled) dataset to its first N rows -
so a smoke run of `model_reasoning --limit 6` always tested `arithmetic-01..06` and nothing else (see
the fix-round-1 brief, E5). `stratified_limit_ids` instead round-robins a seeded per-category shuffle,
so a smoke run of any size still exercises every category it can.
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any


def stratified_limit_ids(rows: list[dict[str, Any]], limit: int | None, seed: int,
                         category_key: str = "category") -> list[str] | None:
    """The ids of at most `limit` rows, spread across `category_key` values.

    None when `limit` is None or is not smaller than `len(rows)`: nothing to trim, use every row.
    Otherwise: group row ids by category (a missing/falsy category becomes its own "" group), shuffle
    each group with `random.Random(seed)` so the specific ids chosen are deterministic per seed, then
    take one id per category per round (categories visited in sorted order) until `limit` ids are
    collected. This guarantees every category contributes at least one id, for as long as any category
    still has ids left, before a second round revisits a category.
    """
    if limit is None or limit >= len(rows):
        return None
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(category_key) or "")].append(row["id"])
    rng = random.Random(seed)
    for ids in groups.values():
        rng.shuffle(ids)
    categories = sorted(groups)
    selected: list[str] = []
    round_index = 0
    while len(selected) < limit:
        progressed = False
        for category in categories:
            bucket = groups[category]
            if round_index < len(bucket):
                selected.append(bucket[round_index])
                progressed = True
                if len(selected) == limit:
                    break
        if not progressed:
            break
        round_index += 1
    return selected


def filter_rows_to_ids(rows: list[dict[str, Any]], ids: list[str] | None) -> list[dict[str, Any]]:
    """`rows` restricted to `ids`, in the order `ids` gives (id-keyed, so callers need not assume
    `rows` order matches). `ids=None` (stratified_limit_ids's "nothing to trim" case) returns `rows`
    unchanged."""
    if ids is None:
        return rows
    by_id = {row["id"]: row for row in rows}
    return [by_id[item_id] for item_id in ids if item_id in by_id]


def sampled_item_ids(items: list[dict[str, Any]], limit: int | None) -> list[str] | None:
    """The item ids actually produced by a suite run, for `summary.json`, when a `--limit` was in
    effect (None otherwise: an unlimited run's full item list is already in items.jsonl)."""
    if limit is None:
        return None
    return sorted({item["item_id"] for item in items})
