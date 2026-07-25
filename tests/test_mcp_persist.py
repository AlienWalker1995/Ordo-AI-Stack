"""Dashboard MCP persistence: a UI enable/disable must ALSO edit ordo.yaml's `plugins:` list, because
servers.txt is render-owned and a re-render would otherwise reseed the toggle away. The surgical edit
MUST preserve every other line + comment, and refuse (fall back) rather than corrupt the source."""
import pytest

pytest.importorskip("fastapi", reason="dashboard runtime deps (fastapi) not present")

from dashboard.app import _edit_plugins_list, _persist_mcp_toggle  # noqa: E402

# A representative ordo.yaml fragment: block-style plugins list with inline comments + surrounding
# top-level keys (mirrors out/ordo.yaml). The comfyui SERVER maps to the comfyui-mcp PLUGIN.
SAMPLE = """\
# top-of-file comment
model: auto

plugins:
  - comfyui           # media backend (5090)
  - qdrant-rag        # MCP: qdrant_search / qdrant_status
  - comfyui-mcp       # MCP: ComfyUI media tools (server_id comfyui -> comfyui__*)
  - searxng           # MCP: web search

# trailing top-level key must be untouched
cloud_fallback:
  enabled: false
"""


def test_disable_removes_plugin_line_and_preserves_comments():
    out = _edit_plugins_list(SAMPLE, "comfyui-mcp", "remove")
    assert "  - comfyui-mcp" not in out
    # every OTHER line + comment survives verbatim
    assert "  - comfyui           # media backend (5090)" in out
    assert "  - qdrant-rag        # MCP: qdrant_search / qdrant_status" in out
    assert "# top-of-file comment" in out
    assert "# trailing top-level key must be untouched" in out
    assert "cloud_fallback:" in out
    # exactly one line removed
    assert len(out.splitlines()) == len(SAMPLE.splitlines()) - 1


def test_enable_readds_plugin_after_last_item_with_matching_indent():
    removed = _edit_plugins_list(SAMPLE, "comfyui-mcp", "remove")
    restored = _edit_plugins_list(removed, "comfyui-mcp", "add")
    assert "  - comfyui-mcp\n" in restored
    # re-added as the LAST item, before the blank line + next top-level key
    body = restored.split("plugins:\n", 1)[1]
    items = [ln for ln in body.splitlines() if ln.startswith("  - ")]
    assert items[-1].strip() == "- comfyui-mcp"
    assert "cloud_fallback:" in restored


def test_add_and_remove_are_idempotent_noops():
    # adding an already-present plugin changes nothing
    assert _edit_plugins_list(SAMPLE, "searxng", "add") == SAMPLE
    # removing an absent plugin changes nothing
    assert _edit_plugins_list(SAMPLE, "not-here", "remove") == SAMPLE


def test_only_the_exact_item_is_removed_no_substring_match():
    # removing `comfyui` must NOT also strip `comfyui-mcp` (distinct plugin ids)
    out = _edit_plugins_list(SAMPLE, "comfyui", "remove")
    assert "  - comfyui-mcp       #" in out          # still present
    assert "\n  - comfyui  " not in out and "- comfyui \n" not in out
    # the comfyui service line is gone, comfyui-mcp stays
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("- comfyui")]
    assert lines == ["  - comfyui-mcp       # MCP: ComfyUI media tools (server_id comfyui -> comfyui__*)"]


def test_inline_flow_list_is_refused():
    # an inline `plugins: [a, b]` cannot be line-edited safely -> refuse (caller falls back)
    flow = "model: auto\nplugins: [comfyui, searxng]\ncloud_fallback:\n  enabled: false\n"
    with pytest.raises(ValueError):
        _edit_plugins_list(flow, "searxng", "remove")


def test_missing_plugins_key_is_refused():
    with pytest.raises(ValueError):
        _edit_plugins_list("model: auto\ncloud_fallback:\n  enabled: false\n", "x", "add")


def test_crlf_line_endings_preserved_on_add():
    crlf = SAMPLE.replace("\n", "\r\n")
    removed = _edit_plugins_list(crlf, "comfyui-mcp", "remove")
    restored = _edit_plugins_list(removed, "comfyui-mcp", "add")
    assert "  - comfyui-mcp\r\n" in restored          # matched the file's CRLF ending


# ── _persist_mcp_toggle: the endpoint-facing orchestrator. Server not in the map => flagged, NOT
#    faked into ordo.yaml; ordo.yaml unmounted => graceful servers.txt-only fallback. ──
def test_persist_rejects_server_not_in_map(monkeypatch, tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr("dashboard.app.ORDO_SOURCE_PATH", str(src))
    monkeypatch.setattr("dashboard.app._read_server_plugin_map",
                        lambda: {"comfyui": "comfyui-mcp", "searxng": "searxng"})
    res = _persist_mcp_toggle("some-random-docker-mcp", "add")
    assert res["persistent"] is False and res["plugin"] is None and "out of scope" in res["note"]
    # ordo.yaml was NOT touched
    assert src.read_text(encoding="utf-8") == SAMPLE


def test_persist_writes_plugin_for_mapped_server(monkeypatch, tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr("dashboard.app.ORDO_SOURCE_PATH", str(src))
    monkeypatch.setattr("dashboard.app._read_server_plugin_map",
                        lambda: {"comfyui": "comfyui-mcp", "searxng": "searxng"})
    # disable the comfyui SERVER -> removes the comfyui-mcp PLUGIN line
    res = _persist_mcp_toggle("comfyui", "remove")
    assert res["persistent"] is True and res["plugin"] == "comfyui-mcp"
    assert "  - comfyui-mcp" not in src.read_text(encoding="utf-8")
    assert "  - comfyui           # media backend (5090)" in src.read_text(encoding="utf-8")


def test_persist_falls_back_when_source_unset(monkeypatch):
    monkeypatch.setattr("dashboard.app.ORDO_SOURCE_PATH", None)
    res = _persist_mcp_toggle("comfyui", "remove")
    assert res["persistent"] is False and "not survive a re-render" in res["note"]
