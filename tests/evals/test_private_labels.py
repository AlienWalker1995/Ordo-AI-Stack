"""E2b: the judge-labelled three-way split of the private domain candidate pool.

`private_dataset.py`'s labelling helpers (candidate merge, label lookup, the pending-label queue,
and validation/merge reused from judge.py) are pure - no inspect_ai import - so they are exercised
directly here, on synthetic candidate ids and ask text only. No operator data appears in this file
(README's privacy rule): every candidate below is a made-up string, never a real Discord message.
"""
from __future__ import annotations

import pytest
from ordo_evals import judge
from ordo_evals import private_dataset as pd


def candidate(item_id, text="an ask", source="hermes-state:discord"):
    return {"id": item_id, "input": text, "source": source}


CANDIDATES = [candidate("pd-aaa", "How does TCP handshaking work?"),
             candidate("pd-bbb", "Can you pull the minmax repo and check the latest PR?"),
             candidate("pd-ccc", "Can you get me a link to that knife?")]


# ── candidate pool: merge is a union by id, idempotent across repeat samples ────

def test_merge_candidates_unions_by_id_and_keeps_the_first_copy():
    merged = pd.merge_candidates([candidate("pd-aaa", "first text")], [candidate("pd-aaa", "second text"),
                                                                        candidate("pd-bbb", "new")])
    assert [c["id"] for c in merged] == ["pd-aaa", "pd-bbb"]
    assert next(c for c in merged if c["id"] == "pd-aaa")["input"] == "first text"


def test_merge_candidates_is_sorted_and_stable_on_repeat_calls():
    once = pd.merge_candidates([], CANDIDATES)
    twice = pd.merge_candidates(once, CANDIDATES)
    assert once == twice
    assert [c["id"] for c in once] == sorted(c["id"] for c in CANDIDATES)


# ── label lookup / counts ────────────────────────────────────────────────────────

def label_row(item_id, label, rationale="r"):
    return {"item_id": item_id, "criterion": "label", "score": label, "scale": judge.PRIVATE_LABEL,
            "rationale": rationale, "suite": "private_domain"}


def mutation_row(item_id, mutation, rationale="r"):
    return {"item_id": item_id, "criterion": "mutation", "score": mutation, "scale": judge.PRIVATE_MUTATION,
            "rationale": rationale, "suite": "private_domain"}


def test_labels_by_id_reads_only_the_label_criterion():
    rows = [label_row("pd-aaa", "self_contained"), {"item_id": "pd-bbb", "criterion": "other", "score": "x"}]
    assert pd.labels_by_id(rows) == {"pd-aaa": "self_contained"}


def test_items_with_label_filters_the_candidate_pool():
    labels = {"pd-aaa": "self_contained", "pd-bbb": "agent_standalone"}
    assert [c["id"] for c in pd.items_with_label(CANDIDATES, labels, pd.LABEL_SELF_CONTAINED)] == ["pd-aaa"]
    assert [c["id"] for c in pd.items_with_label(CANDIDATES, labels, pd.LABEL_AGENT_STANDALONE)] == ["pd-bbb"]
    assert pd.items_with_label(CANDIDATES, labels, pd.LABEL_CONVERSATION_DEPENDENT) == []


def test_label_counts_reports_every_label_plus_unlabeled_and_total():
    labels = {"pd-aaa": "self_contained", "pd-bbb": "agent_standalone"}
    counts = pd.label_counts(CANDIDATES, labels)
    assert counts == {"self_contained": 1, "agent_standalone": 1, "conversation_dependent": 0,
                      "unlabeled": 1, "total": 3}


# ── E11: the second, independent "mutation" labelling dimension (read_only / mutating) ───────

def test_mutations_by_id_reads_only_the_mutation_criterion():
    rows = [mutation_row("pd-aaa", "read_only"), {"item_id": "pd-bbb", "criterion": "label", "score": "x"}]
    assert pd.mutations_by_id(rows) == {"pd-aaa": "read_only"}


def test_items_with_label_and_mutation_requires_both_dimensions_to_match():
    """This is the exact filter harness_domain.run() builds its Inspect dataset from - it is what
    keeps a mutating-labelled item from ever reaching the suite (E11 safety fix)."""
    labels = {"pd-aaa": "agent_standalone", "pd-bbb": "agent_standalone", "pd-ccc": "agent_standalone"}
    mutations = {"pd-aaa": "read_only", "pd-bbb": "mutating"}  # pd-ccc has no mutation label at all
    selected = pd.items_with_label_and_mutation(CANDIDATES, labels, mutations,
                                                label=pd.LABEL_AGENT_STANDALONE, mutation=pd.MUTATION_READ_ONLY)
    assert [c["id"] for c in selected] == ["pd-aaa"]


def test_mutation_counts_reports_read_only_mutating_unlabeled_and_total_among_one_label():
    labels = {"pd-aaa": "agent_standalone", "pd-bbb": "agent_standalone", "pd-ccc": "self_contained"}
    mutations = {"pd-aaa": "read_only"}  # pd-bbb unlabeled; pd-ccc is not agent_standalone at all
    counts = pd.mutation_counts(CANDIDATES, labels, mutations, pd.LABEL_AGENT_STANDALONE)
    assert counts == {"read_only": 1, "mutating": 0, "unlabeled": 1, "total": 2}


# ── the pending-label queue: judge_queue.jsonl-shaped, regenerated fresh each time ──

def test_pending_label_queue_only_lists_candidates_missing_either_dimension_in_queue_entry_shape():
    """E11: a candidate needs BOTH the label and the mutation criterion filled before it drops out of
    the queue - pd-aaa here has a label but no mutation grade yet, so it must stay queued."""
    queue = pd.pending_label_queue(CANDIDATES, {"pd-aaa": "self_contained"}, {})
    assert {entry["item_id"] for entry in queue} == {"pd-aaa", "pd-bbb", "pd-ccc"}
    entry = queue[0]
    assert set(entry) == {"run_id", "suite", "item_id", "criteria", "rubric", "input", "output", "context"}
    assert entry["criteria"] == judge.PRIVATE_LABEL_CRITERIA


def test_pending_label_queue_is_idempotent_once_everything_is_labelled_on_both_dimensions():
    labels = {c["id"]: pd.LABEL_SELF_CONTAINED for c in CANDIDATES}
    mutations = {c["id"]: pd.MUTATION_READ_ONLY for c in CANDIDATES}
    assert pd.pending_label_queue(CANDIDATES, labels, mutations) == []


def test_relabelling_an_existing_candidate_on_either_dimension_works_independently():
    """E11: re-labelling "mutation" for a candidate that already has a "label" grade must not disturb
    that label, and vice versa - each criterion is validated and merged independently."""
    first = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "label", "score": "agent_standalone", "rationale": "r"}], CANDIDATES)[0]
    stored = pd.merge_labels([], first)
    mutation_grade = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "mutation", "score": "mutating", "rationale": "r"}], CANDIDATES)[0]
    stored = pd.merge_labels(stored, mutation_grade)
    assert pd.labels_by_id(stored) == {"pd-aaa": "agent_standalone"}
    assert pd.mutations_by_id(stored) == {"pd-aaa": "mutating"}

    relabel_mutation = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "mutation", "score": "read_only", "rationale": "r"}], CANDIDATES)[0]
    stored = pd.merge_labels(stored, relabel_mutation)
    assert pd.labels_by_id(stored) == {"pd-aaa": "agent_standalone"}  # unchanged
    assert pd.mutations_by_id(stored) == {"pd-aaa": "read_only"}      # replaced


# ── validation and merge (reused wholesale from judge.py) ───────────────────────

def test_validate_labels_accepts_a_known_id_and_a_valid_label():
    valid, errors = pd.validate_labels([{"item_id": "pd-aaa", "criterion": "label", "score": "self_contained",
                                        "rationale": "general knowledge"}], CANDIDATES)
    assert errors == []
    assert valid == [{"item_id": "pd-aaa", "criterion": "label", "score": "self_contained",
                      "scale": judge.PRIVATE_LABEL, "rationale": "general knowledge", "suite": "private_domain"}]


@pytest.mark.parametrize("bad_score", ["maybe", "tool_directed", ""])
def test_validate_labels_rejects_an_unknown_label_value(bad_score):
    valid, errors = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "label", "score": bad_score, "rationale": "r"}], CANDIDATES)
    assert valid == [] and any("must be one of" in e for e in errors)


def test_validate_labels_is_case_insensitive_like_every_other_judge_scale():
    valid, errors = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "label", "score": "Self_Contained", "rationale": "r"}], CANDIDATES)
    assert errors == [] and valid[0]["score"] == "self_contained"


def test_validate_labels_rejects_an_id_outside_the_candidate_pool():
    valid, errors = pd.validate_labels(
        [{"item_id": "pd-not-a-candidate", "criterion": "label", "score": "self_contained", "rationale": "r"}],
        CANDIDATES)
    assert valid == [] and any("not in this run's judge queue" in e for e in errors)


def test_validate_labels_rejects_a_missing_rationale():
    valid, errors = pd.validate_labels(
        [{"item_id": "pd-aaa", "criterion": "label", "score": "self_contained", "rationale": "  "}], CANDIDATES)
    assert valid == [] and any("rationale" in e for e in errors)


def test_merge_labels_lets_a_relabel_win_and_ingest_labels_stays_idempotent():
    first = pd.validate_labels([{"item_id": "pd-aaa", "criterion": "label", "score": "conversation_dependent",
                                 "rationale": "looked like a follow-up"}], CANDIDATES)[0]
    stored = pd.merge_labels([], first)
    relabel = pd.validate_labels([{"item_id": "pd-aaa", "criterion": "label", "score": "self_contained",
                                   "rationale": "actually reads fine alone"}], CANDIDATES)[0]
    stored = pd.merge_labels(stored, relabel)
    # re-ingesting the SAME relabel a second time changes nothing further (idempotent).
    stored_again = pd.merge_labels(stored, relabel)
    assert pd.labels_by_id(stored) == pd.labels_by_id(stored_again) == {"pd-aaa": "self_contained"}


# ── judge.py: the criteria/rubric additions E2b needs ────────────────────────────

def test_agent_domain_criteria_extends_domain_criteria_with_used_tools():
    assert judge.AGENT_DOMAIN_CRITERIA == {**judge.DOMAIN_CRITERIA, "used_tools": judge.PASS_FAIL}
    assert "used_tools" in judge.AGENT_DOMAIN_RUBRIC


def test_private_label_is_a_registered_scale_with_exactly_three_values():
    assert judge.SCALE_VALUES[judge.PRIVATE_LABEL] == {
        "self_contained", "agent_standalone", "conversation_dependent"}


def test_private_mutation_is_a_registered_scale_with_exactly_two_values():
    """E11: the two labelling criteria are queued together on every private domain candidate."""
    assert judge.SCALE_VALUES[judge.PRIVATE_MUTATION] == {"read_only", "mutating"}
    assert judge.PRIVATE_LABEL_CRITERIA == {"label": judge.PRIVATE_LABEL, "mutation": judge.PRIVATE_MUTATION}
    assert "mutation" in judge.PRIVATE_LABEL_RUBRIC and "read_only" in judge.PRIVATE_LABEL_RUBRIC
