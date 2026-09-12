"""The MCP parity checker: prefix stripping and superset comparison against the captured baseline."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_parity_check import compare, strip_prefix  # noqa: E402

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
