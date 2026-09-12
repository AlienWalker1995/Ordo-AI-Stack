"""The MCP parity checker: prefix stripping and superset comparison against the captured baseline."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_parity_check import compare, load_renames, strip_prefix  # noqa: E402

BASELINE = json.loads((ROOT / "tests" / "fixtures" / "mcp_tool_baseline_2026-09-11.json").read_text())
# LiteLLM server NAMES (the tool prefix): the hyphenated server ids with hyphens -> underscores.
SERVER_IDS = {"codebase_memory", "comfyui", "memory_vault", "n8n", "orchestration", "qdrant_rag", "searxng"}


def test_baseline_fixture_has_75_tools():
    assert BASELINE["count"] == 75 and len(BASELINE["tools"]) == 75


def test_strip_prefix_removes_only_a_known_server_prefix():
    assert strip_prefix("memory_vault-read_note", SERVER_IDS) == "read_note"
    assert strip_prefix("qdrant_rag-qdrant_search", SERVER_IDS) == "qdrant_search"
    # an unknown prefix is left alone (never guess)
    assert strip_prefix("other-tool", SERVER_IDS) == "other-tool"
    # the longest matching server name wins (memory_vault, not a hypothetical `memory`)
    assert strip_prefix("n8n-n8n_health_check", SERVER_IDS) == "n8n_health_check"


def test_compare_is_a_superset_check_with_required_extras():
    baseline = [t["name"] for t in BASELINE["tools"]]
    current = [f"memory_vault-{n}" if n == "read_note" else f"searxng-{n}" for n in baseline]
    rep = compare(baseline, current, SERVER_IDS, required_extra=["get_system_stats"])
    assert rep["ok"] is False
    assert "get_system_stats" in rep["missing"]
    current.append("comfyui-get_system_stats")
    rep = compare(baseline, current, SERVER_IDS, required_extra=["get_system_stats"])
    assert rep["ok"] is True and rep["missing"] == []


# ── Upstream renames: mcp/n8n 2.22.18 -> n8n-mcp 2.84.1 ───────────────────────────────────────────
RENAMES_PATH = ROOT / "tests" / "fixtures" / "mcp_tool_baseline_2026-09-11.upstream-renames.json"
RENAMES = json.loads(RENAMES_PATH.read_text(encoding="utf-8"))


def test_rename_to_a_present_successor_is_ok():
    rep = compare(["list_nodes"], ["n8n-search_nodes"], SERVER_IDS, required_extra=[],
                  renames={"list_nodes": "search_nodes"})
    assert rep["ok"] is True
    assert rep["missing"] == []
    assert rep["renamed_ok"] == ["list_nodes -> search_nodes"]
    assert rep["removed_upstream"] == []


def test_rename_whose_successor_is_absent_is_still_missing():
    rep = compare(["list_nodes"], ["n8n-get_node"], SERVER_IDS, required_extra=[],
                  renames={"list_nodes": "search_nodes"})
    assert rep["ok"] is False
    assert rep["missing"] == ["list_nodes"]
    assert rep["renamed_ok"] == []


def test_null_rename_counts_as_removed_upstream_and_is_ok():
    rep = compare(["list_ai_tools"], ["n8n-search_nodes"], SERVER_IDS, required_extra=[],
                  renames={"list_ai_tools": None})
    assert rep["ok"] is True
    assert rep["missing"] == []
    assert rep["removed_upstream"] == ["list_ai_tools"]
    assert rep["renamed_ok"] == []


def test_a_baseline_name_with_no_rename_entry_is_still_missing():
    rep = compare(["list_nodes"], ["n8n-search_nodes"], SERVER_IDS, required_extra=[], renames={})
    assert rep["ok"] is False and rep["missing"] == ["list_nodes"]


def test_renames_fixture_keys_are_all_baseline_names():
    baseline = {t["name"] for t in BASELINE["tools"]}
    mapping = RENAMES["n8n"]
    assert mapping, "the n8n rename map must not be empty"
    assert set(mapping) <= baseline, f"not baseline names: {sorted(set(mapping) - baseline)}"
    assert RENAMES["_note"].startswith("upstream mcp/n8n 2.22.18 -> n8n-mcp 2.84.1")


def test_load_renames_flattens_servers_and_skips_notes():
    flat = load_renames(str(RENAMES_PATH))
    assert flat["list_nodes"] == "search_nodes"
    assert flat["get_database_statistics"] is None
    assert "_note" not in flat
