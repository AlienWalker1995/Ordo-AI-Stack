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
    assert len(items) == 40
    assert {i["category"] for i in items} == {"arithmetic", "units", "dates", "logic"}
    for entry in items:
        assert entry["question"].strip() and str(entry["answer"]).strip()
        assert isinstance(entry.get("aliases", []), list)


def test_toolcall_dataset_shape_and_expectations_reference_offered_tools():
    items = rows(DATASETS / "toolcall.jsonl")
    assert len(items) == 40
    assert {i["category"] for i in items} == {"single_call", "parallel", "multi_turn", "no_tool",
                                              "arg_types", "enum"}
    for entry in items:
        schemas = tool_schemas(entry["tools"])
        assert schemas, f"{entry['id']} offers no tools"
        assert entry["messages"] and entry["messages"][-1]["role"] in {"user", "tool"}
        for call in entry["expected"]["calls"]:
            assert call["name"] in schemas, f"{entry['id']} expects a call to an unoffered tool"
            unknown = set(call.get("arguments", {})) - set(schemas[call["name"]].get("properties", {}))
            assert not unknown, f"{entry['id']} expects arguments {unknown} that are not in the schema"


def test_harness_ops_dataset_shape_and_checks_exist():
    items = rows(DATASETS / "harness_ops.jsonl")
    assert len(items) == 15
    for entry in items:
        assert entry["check"]["type"] in checks.CHECK_TYPES
        for step in entry.get("setup", []) or []:
            assert step["type"] == "seed_note"
            assert step["path"].startswith("{vault_dir}/")
        # every template placeholder must be one the runner supplies
        context = checks.item_context("run-1", entry["id"])
        checks.build_prompt(entry, context)
        checks.render(entry["check"], context)


def test_harness_honesty_dataset_is_impossible_by_construction():
    """Most items name a per-run nonce so the target cannot exist by accident; the two that do not
    (a command that exits non-zero, data the agent has no access to) are impossible on their own."""
    items = rows(DATASETS / "harness_honesty.jsonl")
    assert len(items) == 8
    without_nonce = {"hon-06-failing-command", "hon-08-no-access"}
    for entry in items:
        assert entry["precondition"]["type"] in {"none", "vault_missing", "qdrant_collection_missing"}
        context = checks.item_context("run-1", entry["id"])
        prompt = checks.build_prompt(entry, context)
        assert context["nonce"] in prompt or entry["id"] in without_nonce


def test_no_dataset_asks_for_gpu_work():
    """The scheduler-lease rule: an eval must never start a render (see services/evals/README.md)."""
    forbidden = re.compile(r"(?i)\b(comfyui|render an image|generate an image|txt2img|img2vid|video generation)\b")
    for path in FILES:
        assert not forbidden.search(path.read_text(encoding="utf-8")), f"{path.name} asks for GPU work"
