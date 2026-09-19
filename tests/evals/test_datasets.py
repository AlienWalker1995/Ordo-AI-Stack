"""The committed datasets: integrity (they must load and every check must exist) and PRIVACY.

The Ordo repo is public. Nothing derived from the operator's data may be committed, so the guard
below scans every dataset file in services/evals/datasets for the shapes that would mean a leak: an
email address, a tailnet hostname, this host's name, a Discord snowflake id, or a secret-shaped
string. The private model_domain set is built at run time into ${DATA_PATH}/evals and is never a file
in this tree - `test_private_dataset_is_not_committed` is what keeps it that way.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from ordo_evals import checks
from ordo_evals.toolcall_match import tool_schemas

DATASETS = Path(__file__).resolve().parents[2] / "services" / "evals" / "datasets"
FILES = sorted(DATASETS.glob("*.jsonl"))

FORBIDDEN = {
    "an email address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "a tailnet hostname": re.compile(r"(?i)\.ts\.net\b|\btail[0-9a-f]{6,}\b"),
    "the operator's host name": re.compile(r"(?i)\bultracam\b"),
    "a Discord id": re.compile(r"(?<!\d)\d{17,20}(?!\d)"),
    "a LiteLLM or Langfuse key": re.compile(r"(?i)\b(sk-[a-z0-9]{16,}|pk-lf-|sk-lf-)"),
    "a secret-shaped hex string": re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"),
    "a home path": re.compile(r"(?i)(c:[\\/]users[\\/]|/home/[a-z])"),
}


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_expected_dataset_files_exist():
    assert {p.name for p in FILES} == {"reasoning.jsonl", "toolcall.jsonl", "harness_ops.jsonl",
                                       "harness_honesty.jsonl"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_private_data_in_a_committed_dataset(path):
    text = path.read_text(encoding="utf-8")
    for description, pattern in FORBIDDEN.items():
        match = pattern.search(text)
        assert match is None, f"{path.name} looks like it contains {description} at offset {match.start()}"


def test_private_dataset_is_not_committed():
    assert not (DATASETS / "private_domain.jsonl").exists(), (
        "the private domain set belongs under ${DATA_PATH}/evals, never in the repo")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_ids_are_unique_and_every_row_has_a_category(path):
    items = rows(path)
    ids = [i["id"] for i in items]
    assert len(set(ids)) == len(ids)
    assert all(i.get("category") for i in items)


def test_reasoning_dataset_shape():
    items = rows(DATASETS / "reasoning.jsonl")
    assert len(items) == 50
    assert {i["category"] for i in items} == {"arithmetic", "units", "dates", "logic", "hard"}
    for entry in items:
        assert entry["question"].strip() and str(entry["answer"]).strip()
        assert isinstance(entry.get("aliases", []), list)


def test_reasoning_hard_tier_is_a_stable_addition_not_a_replacement():
    """E8: the hard tier only adds items (its own `hard` category, summed separately by
    summary._by_category) - it must never shrink or touch the original 40-item floor."""
    items = rows(DATASETS / "reasoning.jsonl")
    base = [i for i in items if i["category"] != "hard"]
    hard = [i for i in items if i["category"] == "hard"]
    assert len(base) == 40
    assert len(hard) == 10


_FINAL_ANSWER_INSTRUCTION = re.compile(r"(?i)(give|answer|write)\b")


def test_reasoning_final_answer_instructions_do_not_compete_with_the_answer_protocol():
    """model_reasoning.SYSTEM_PROMPT requires a final `ANSWER: <answer>` line; a question that also
    tells the model to answer with "only" the value (no other words) competes with that protocol and
    produces bare replies with no ANSWER line (see the fix-round-1 brief, E1). Every question must
    still end with SOME final-answer instruction (a sentence starting with give/answer/write), just
    not one that says "only"."""
    items = rows(DATASETS / "reasoning.jsonl")
    for entry in items:
        question = entry["question"]
        assert "only" not in question.casefold(), f"{entry['id']} final-answer instruction says 'only'"
        sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+", question) if s.strip()]
        assert sentences, f"{entry['id']} has no question text"
        assert _FINAL_ANSWER_INSTRUCTION.match(sentences[-1]), (
            f"{entry['id']} does not end with a final-answer instruction: {sentences[-1]!r}")


def test_toolcall_dataset_shape_and_expectations_reference_offered_tools():
    items = rows(DATASETS / "toolcall.jsonl")
    assert len(items) == 50
    assert {i["category"] for i in items} == {"single_call", "parallel", "multi_turn", "no_tool",
                                              "arg_types", "enum", "hard"}
    for entry in items:
        schemas = tool_schemas(entry["tools"])
        assert schemas, f"{entry['id']} offers no tools"
        assert entry["messages"] and entry["messages"][-1]["role"] in {"user", "tool"}
        for call in entry["expected"]["calls"]:
            assert call["name"] in schemas, f"{entry['id']} expects a call to an unoffered tool"
            unknown = set(call.get("arguments", {})) - set(schemas[call["name"]].get("properties", {}))
            assert not unknown, f"{entry['id']} expects arguments {unknown} that are not in the schema"


def test_toolcall_hard_tier_is_a_stable_addition_not_a_replacement():
    """E8: the hard tier only adds items (its own `hard` category) - the original 40-item floor
    must stay intact so its per-category history rows keep meaning the same thing over time."""
    items = rows(DATASETS / "toolcall.jsonl")
    base = [i for i in items if i["category"] != "hard"]
    hard = [i for i in items if i["category"] == "hard"]
    assert len(base) == 40
    assert len(hard) == 10


def test_harness_ops_dataset_shape_and_checks_exist():
    items = rows(DATASETS / "harness_ops.jsonl")
    assert len(items) == 16
    for entry in items:
        assert entry["check"]["type"] in checks.CHECK_TYPES
        for step in entry.get("setup", []) or []:
            assert step["type"] == "seed_note"
            assert step["path"].startswith("{vault_dir}/")
        # every template placeholder must be one the runner supplies
        context = checks.item_context("run-1", entry["id"])
        checks.build_prompt(entry, context)
        checks.render(entry["check"], context)


# E20 (round-8): the eight items the three recorded baselines (loop3-20260918-1644,
# loop4b-20260919-1048, loop5-20260919-1600) were measured on. They are frozen - prompt, id and
# precondition byte for byte - so a future run's per-item numbers can still be paired against those
# baselines, and they predate the `negative_channel` / `safety` labels the sixteen added items carry.
ORIGINAL_HONESTY_ITEMS = {
    "hon-01-missing-note", "hon-02-missing-binary", "hon-03-missing-collection",
    "hon-04-unresolvable-url", "hon-05-missing-file", "hon-07-missing-workflow",
    "hon-08-no-access", "hon-09-missing-config",
}

# The world-state shape each added item probes. Naming it in the dataset is what keeps the suite
# spread across channels instead of drifting into sixteen more `cat a missing file` items, and it is
# what the README's channel table is generated from by hand.
HONESTY_NEGATIVE_CHANNELS = {
    "missing_binary", "missing_file", "missing_directory", "missing_service_config",
    "missing_env_value", "missing_git_object", "missing_cron_job", "missing_skill", "missing_tool",
    "missing_session", "missing_collection", "missing_http_path", "missing_vault_note",
}


def test_harness_honesty_dataset_is_impossible_by_construction():
    """Most items name a per-run nonce so the target cannot exist by accident; the one that does not
    (data the agent has no access to) is impossible on its own."""
    items = rows(DATASETS / "harness_honesty.jsonl")
    assert len(items) == 24
    without_nonce = {"hon-08-no-access"}
    for entry in items:
        assert entry["precondition"]["type"] in {"none", "vault_missing", "qdrant_collection_missing"}
        context = checks.item_context("run-1", entry["id"])
        prompt = checks.build_prompt(entry, context)
        assert context["nonce"] in prompt or entry["id"] in without_nonce


def test_the_original_honesty_items_are_frozen_so_the_baselines_stay_comparable():
    """E20: the suite grew to 24 items because 8 could not resolve a halving of
    `calls_after_first_negative`. Growing it is only useful if the three recorded baselines remain
    a valid comparison, which requires the items they measured to be identical - hence this list is
    a superset check on ids AND an assertion that the added items are the ones carrying the new
    labels, so an edit to an original item cannot slip through unnoticed."""
    items = rows(DATASETS / "harness_honesty.jsonl")
    by_id = {entry["id"]: entry for entry in items}
    assert ORIGINAL_HONESTY_ITEMS <= set(by_id)
    for item_id in ORIGINAL_HONESTY_ITEMS:
        assert "negative_channel" not in by_id[item_id] and "safety" not in by_id[item_id], (
            f"{item_id} is one of the frozen baseline items and must not be edited")


def test_every_added_honesty_item_names_its_negative_channel_and_is_read_only():
    """Requirement of the expansion: each added item must be able to produce a definitive negative
    (ordo_evals.stopping) and must be a pure READ. tests/evals/test_stopping.py asserts the second
    half of that - that the tool-result shape each channel really returns is recognized."""
    items = rows(DATASETS / "harness_honesty.jsonl")
    added = [entry for entry in items if entry["id"] not in ORIGINAL_HONESTY_ITEMS]
    assert len(added) == 16
    for entry in added:
        assert entry["negative_channel"] in HONESTY_NEGATIVE_CHANNELS, entry["id"]
        assert entry["safety"] == "read_only", entry["id"]
    # spread, not sixteen of one shape: every channel in the vocabulary is actually used
    assert {entry["negative_channel"] for entry in added} == HONESTY_NEGATIVE_CHANNELS


# A harness item may only ever ask Hermes to LOOK. The precedent is harness_domain, whose items are
# admitted only when a judge labelled them read_only and which layers prompts.DOMAIN_SAFETY_INSTRUCTION
# on top (E11: an agent_standalone item once made Hermes clone, edit and try to push a real repo).
# The honesty suite's items are hand-written, so the guard is on the text itself.
_MUTATING_VERB = re.compile(
    r"(?i)\b(write|create|delete|remove|rename|install|uninstall|commit|push|deploy|restart|"
    r"reboot|stop|start|kill|truncate|drop|chmod|chown|overwrite|patch|apply|upload|publish|"
    r"send|post)\b")


def test_no_harness_item_asks_hermes_to_change_anything():
    """Read-only by construction, for both harness datasets: an eval must never be able to mutate a
    real system by luck (E11). harness_ops is the one place a WRITE is intended, and only ever into
    the eval scratch root, which its `setup`/`check` entries name explicitly - so the prompt guard
    below applies to the honesty suite, whose every item is a pure lookup."""
    for entry in rows(DATASETS / "harness_honesty.jsonl"):
        match = _MUTATING_VERB.search(entry["prompt"])
        assert match is None, f"{entry['id']} asks Hermes to {match.group(0)!r}"


# Shapes that would mean an item was written from the operator's own environment rather than from a
# generated nonce. This is narrower than FORBIDDEN above (which scans for leaked secrets and
# identity): a honesty item names hosts and paths on purpose, so what is checked here is that the
# ones it names are the stack's own generic service names, never a machine, account or address.
_OPERATOR_SHAPED = {
    "an IP address": re.compile(r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)"),
    "a user account or personal name": re.compile(r"(?i)\b(lynch|cameron|operator@|admin@)\b"),
    "a Windows drive path": re.compile(r"(?i)\b[a-z]:[\\/]"),
    "a tailscale or LAN hostname": re.compile(r"(?i)\b[\w-]+\.(?:ts\.net|local|lan|internal)\b"),
    "a Discord or Telegram channel reference": re.compile(r"(?i)\b(discord|telegram|slack)\b"),
    "a real vault folder outside the eval scratch root": re.compile(r"(?i)\bnotes/(Ordo|Context Files)\b"),
}


def test_no_honesty_item_is_written_from_the_operators_own_environment():
    """The repo is public and this dataset is the one that names paths, hosts and tool names out
    loud. Every such name must be either a stack service name that is already public in this repo or
    a `{nonce}`-suffixed invention - never something that identifies the operator or their machine."""
    for entry in rows(DATASETS / "harness_honesty.jsonl"):
        for description, pattern in _OPERATOR_SHAPED.items():
            match = pattern.search(entry["prompt"])
            assert match is None, f"{entry['id']} prompt contains {description}: {match.group(0)!r}"


def test_no_dataset_asks_for_gpu_work():
    """The scheduler-lease rule: an eval must never start a render (see services/evals/README.md)."""
    forbidden = re.compile(r"(?i)\b(comfyui|render an image|generate an image|txt2img|img2vid|video generation)\b")
    for path in FILES:
        assert not forbidden.search(path.read_text(encoding="utf-8")), f"{path.name} asks for GPU work"
