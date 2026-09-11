"""MCP dashboard API contract on top of LiteLLM's MCP gateway: enabled servers come from the rendered
servers.json, health from LiteLLM's /v1/mcp/server/health + tools/list server_outcomes (a crashed
server can no longer be reported ok), toggles persist to ordo.yaml only."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import _parse_sse_json, app  # noqa: E402

client = TestClient(app)

SERVERS_JSON = {
    "servers": [
        {"id": "searxng", "plugin_id": "searxng", "service": "mcp-searxng", "url": "http://mcp-searxng:8080/mcp",
         "network": "stack", "tools": ["searxng_web_search"], "hosted": False, "name": "SearXNG"},
        {"id": "comfyui", "plugin_id": "comfyui-mcp", "service": "mcp-comfyui", "url": "http://mcp-comfyui:9000/mcp",
         "network": "stack", "tools": ["get_queue"], "hosted": False, "name": "ComfyUI"},
    ],
    "plugin_map": {"searxng": "searxng", "comfyui": "comfyui-mcp", "n8n": "n8n"},
}


def test_parse_sse_json_reads_data_frames_and_plain_json():
    assert _parse_sse_json('event: message\ndata: {"a": 1}\n\ndata: {"b": 2}\n') == [{"a": 1}, {"b": 2}]
    assert _parse_sse_json('{"a": 1}') == [{"a": 1}]


def test_mcp_servers_lists_enabled_and_configured_from_servers_json():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._ordo_source_path", return_value=__import__("pathlib").Path("/tmp/ordo.yaml")):
        r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] == ["searxng", "comfyui"]
    assert d["configured"] == ["comfyui", "n8n", "searxng"]      # every registered kind=mcp plugin's server id
    assert d["catalog"] == []                                    # the Docker online catalog is gone
    assert d["dynamic"] is True and d["ok"] is True
    assert d["registry"]["servers"]["searxng"]["url"] == "http://mcp-searxng:8080/mcp"


def test_mcp_health_marks_a_server_ok_only_when_healthy_and_serving_tools():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._litellm_mcp_health",
               new=AsyncMock(return_value={"searxng": "healthy", "comfyui": "unhealthy"})), \
         patch("dashboard.app._litellm_mcp_outcomes", new=AsyncMock(return_value=(True, {
             "searxng": {"status": "ok", "tool_count": 4}, "comfyui": {"status": "unreachable"}}, None))):
        r = client.get("/api/mcp/health")
    d = r.json()
    assert d["ok"] is True and d["gateway"] == "reachable"
    by_id = {s["id"]: s for s in d["servers"]}
    assert by_id["searxng"] == {"id": "searxng", "ok": True, "status": "healthy", "error": None, "tool_count": 4}
    assert by_id["comfyui"]["ok"] is False and by_id["comfyui"]["status"] == "unhealthy"
    assert "unreachable" in by_id["comfyui"]["error"]


def test_mcp_health_gateway_down_marks_everything_down():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._litellm_mcp_health", new=AsyncMock(return_value={})), \
         patch("dashboard.app._litellm_mcp_outcomes", new=AsyncMock(return_value=(False, {}, "connect refused"))):
        d = client.get("/api/mcp/health").json()
    assert d["ok"] is False and d["gateway"] == "unreachable" and d["gateway_error"] == "connect refused"
    assert all(s["ok"] is False for s in d["servers"])


def test_mcp_add_persists_to_ordo_yaml_and_reports_render_needed():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._persist_mcp_toggle",
               return_value={"persistent": True, "plugin": "n8n", "note": None}) as p:
        r = client.post("/api/mcp/add", json={"server": "n8n"})
    d = r.json()
    p.assert_called_once_with("n8n", "add")
    assert d["status"] == "added" and d["applied"] is False and "ordo render" in d["next"]
    assert d["servers"] == ["searxng", "comfyui", "n8n"]


def test_mcp_add_rejects_a_server_that_is_not_a_registered_plugin():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON):
        r = client.post("/api/mcp/add", json={"server": "duckduckgo"})
    assert r.status_code == 400 and "not a registered" in r.json()["detail"]


def test_mcp_remove_persists_and_reports_render_needed():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._persist_mcp_toggle",
               return_value={"persistent": True, "plugin": "searxng", "note": None}):
        d = client.post("/api/mcp/remove", json={"server": "searxng"}).json()
    assert d["status"] == "removed" and d["applied"] is False and d["servers"] == ["comfyui"]
