"""E12 (round-4 fix): secret-shaped substrings must never be persisted (items.jsonl,
judge_queue.jsonl, Langfuse) verbatim. Synthetic strings only - see the module docstring in
redact.py and the README's privacy section (no operator or real credential text in this repo, ever,
even as a "this is what got redacted" example)."""
from __future__ import annotations

from ordo_evals import redact


def test_github_token_prefixes_are_redacted():
    for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
        text = f"here is the token: {prefix}{'a1B2c3D4' * 3}"
        out = redact.redact_secrets(text)
        assert prefix not in out
        assert "[REDACTED:github_token]" in out


def test_github_fine_grained_pat_is_redacted():
    text = f"use github_pat_{'x' * 40} to clone"
    out = redact.redact_secrets(text)
    assert "github_pat_" not in out
    assert "[REDACTED:github_pat]" in out


def test_sk_style_api_keys_are_redacted():
    for value in ("sk-" + "a" * 20, "sk-ant-" + "b" * 20, "sk-proj-" + "c" * 20):
        out = redact.redact_secrets(f"the key is {value} - keep it safe")
        assert value not in out
        assert "[REDACTED:api_key]" in out


def test_langfuse_key_prefixes_are_redacted():
    for value in ("pk-lf-" + "1" * 10, "sk-lf-" + "2" * 10):
        out = redact.redact_secrets(f"public key {value}")
        assert value not in out
        assert "[REDACTED:langfuse_key]" in out


def test_a_long_hex_run_next_to_a_key_word_is_redacted():
    value = "d3adbeef" * 5  # 40 hex chars
    for text in (f"api_key: {value}", f"the token={value}", f"{value} is the secret", f"{value} (bearer)"):
        out = redact.redact_secrets(text)
        assert value not in out
        assert "[REDACTED:key_adjacent_value]" in out


def test_a_bare_long_hex_or_base64_run_with_no_key_word_is_left_alone():
    """A 32+ char hex string on its own is often a legitimate hash/id (a commit SHA, a trace id) -
    only redact one when it sits next to a key-ish word."""
    commit_sha = "a" * 40
    text = f"fixed in commit {commit_sha}"
    assert redact.redact_secrets(text) == text


def test_ordinary_text_is_unchanged():
    text = "The command exited with code 3, so it did not succeed."
    assert redact.redact_secrets(text) == text


def test_none_and_non_strings_pass_through():
    assert redact.redact_secrets(None) is None
    assert redact.redact_value(None) is None
    assert redact.redact_value(True) is True
    assert redact.redact_value(42) == 42


def test_redact_value_recurses_through_dicts_and_lists():
    value = {"a": [f"token: ghp_{'x' * 20}", {"b": "fine"}], "c": 3, "d": None}
    out = redact.redact_value(value)
    assert "ghp_" not in out["a"][0]
    assert out["a"][1]["b"] == "fine"
    assert out["c"] == 3 and out["d"] is None


# ── redact_item / redact_queue_entry: structural fields must never be touched ───────────────────

def test_redact_item_leaves_structural_fields_alone_and_redacts_free_text():
    item = {
        "run_id": "r1", "suite": "harness_ops", "subject": "harness", "item_id": "ops-01",
        "trace_id": "a1b2c3d4" * 4,  # a real 32-char hex trace id - must survive untouched
        "input": "run the thing", "output": f"done, token: ghp_{'z' * 20}",
        "error": None, "scores": {"artifact_ok": True},
        "metadata": {"session_id": "eval-r1-harness_ops-ops-01", "hermes_error": None,
                     "trajectory": {"last_assistant_message": f"secret={'d3ad' * 10}"}},
        "infra_error": False,
    }
    out = redact.redact_item(item)
    assert out["trace_id"] == item["trace_id"]
    assert out["item_id"] == item["item_id"] and out["run_id"] == item["run_id"]
    assert "ghp_" not in out["output"] and "[REDACTED:github_token]" in out["output"]
    assert "[REDACTED:key_adjacent_value]" in out["metadata"]["trajectory"]["last_assistant_message"]
    assert out["metadata"]["session_id"] == "eval-r1-harness_ops-ops-01"  # untouched
    # redact_item never mutates the caller's dict in place
    assert "ghp_" in item["output"]


def test_redact_queue_entry_redacts_input_output_and_context_but_not_rubric():
    entry = {
        "run_id": "r1", "suite": "harness_domain", "item_id": "pd-1",
        "criteria": {"correctness": "likert5"}, "rubric": "Grade the answer. sk- is not a real key here.",
        "input": "what is my github_pat_" + "y" * 30 + "?",
        "output": f"it is sk-{'w' * 20}", "context": {"tools_used": [f"token ghp_{'q' * 20}"]},
    }
    out = redact.redact_queue_entry(entry)
    assert out["rubric"] == entry["rubric"]  # static template text, never redacted
    assert "github_pat_" not in out["input"]
    assert "sk-" not in out["output"] or "[REDACTED" in out["output"]
    assert "ghp_" not in out["context"]["tools_used"][0]
    assert out["item_id"] == "pd-1" and out["criteria"] == {"correctness": "likert5"}
