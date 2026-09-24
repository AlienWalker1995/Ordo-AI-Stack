"""MCP dashboard API contract on top of LiteLLM's MCP gateway: enabled servers come from the rendered
servers.json, health from LiteLLM's /v1/mcp/server/health + tools/list server_outcomes (a crashed
server can no longer be reported ok), toggles persist to ordo.yaml through ops-controller only."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import _join_mcp_health, _mcp_rows, _parse_sse_json, app  # noqa: E402

client = TestClient(app)

SERVERS_JSON = {
    "servers": [
        {"id": "searxng", "litellm_name": "searxng", "plugin_id": "searxng", "service": "mcp-searxng",
         "url": "http://mcp-searxng:8080/mcp",
         "network": "stack", "tools": ["searxng_web_search"], "hosted": False, "name": "SearXNG"},
        {"id": "comfyui", "litellm_name": "comfyui", "plugin_id": "comfyui-mcp", "service": "mcp-comfyui",
         "url": "http://mcp-comfyui:9000/mcp",
         "network": "stack", "tools": ["get_queue"], "hosted": False, "name": "ComfyUI"},
        # a hyphenated server id: LiteLLM knows it by its underscore name, the API row keeps the id
        {"id": "memory-vault", "litellm_name": "memory_vault", "plugin_id": "memory-vault",
         "service": "mcp-memory-vault", "url": "http://mcp-memory-vault:9000/mcp",
         "network": "internal", "tools": ["read_note"], "hosted": False, "name": "Memory Vault"},
    ],
    "plugin_map": {"searxng": "searxng", "comfyui": "comfyui-mcp", "n8n": "n8n"},
}


def test_parse_sse_json_reads_data_frames_and_plain_json():
    assert _parse_sse_json('event: message\ndata: {"a": 1}\n\ndata: {"b": 2}\n') == [{"a": 1}, {"b": 2}]
    assert _parse_sse_json('{"a": 1}') == [{"a": 1}]


def test_join_mcp_health_keys_status_by_server_name_not_the_hashed_id():
    """LiteLLM 1.100.1: /v1/mcp/server/health carries only the hashed server_id, /v1/mcp/server the
    name. The join is what makes the health map look-uppable by litellm_name."""
    servers_rows = [
        {"server_id": "f71782dd2538b57e2044da9f9c7ade76", "server_name": "qdrant_rag", "alias": None},
        {"server_id": "aa11", "server_name": "memory_vault", "alias": None},
        {"server_id": "bb22", "server_name": None, "alias": "comfyui"},   # alias wins when unnamed
        {"server_id": "cc33", "server_name": "n8n", "alias": None},       # no health row -> absent
    ]
    health_rows = [
        {"server_id": "f71782dd2538b57e2044da9f9c7ade76", "status": "healthy"},
        {"server_id": "aa11", "status": "unhealthy"},
        {"server_id": "bb22"},                                            # no status -> unknown
        {"server_id": "dd44", "status": "healthy"},                       # unknown id -> dropped
    ]
    assert _join_mcp_health(servers_rows, health_rows) == {
        "qdrant_rag": "healthy", "memory_vault": "unhealthy", "comfyui": "unknown"}


def test_join_mcp_health_is_empty_when_either_side_is_empty():
    assert _join_mcp_health([], [{"server_id": "aa11", "status": "healthy"}]) == {}
    assert _join_mcp_health([{"server_id": "aa11", "server_name": "n8n"}], []) == {}


def test_mcp_rows_accepts_a_bare_list_or_an_envelope_and_drops_junk():
    rows = [{"server_id": "aa11"}]
    assert _mcp_rows(rows) == rows
    assert _mcp_rows({"servers": rows}) == rows
    assert _mcp_rows({"data": rows}) == rows
    assert _mcp_rows({"unexpected": 1}) == [] and _mcp_rows(None) == []
    assert _mcp_rows(["not-a-dict", {"server_id": "aa11"}]) == rows


def test_mcp_servers_lists_enabled_and_configured_from_servers_json():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app.OPS_CONTROLLER_TOKEN", "token"):
        r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] == ["searxng", "comfyui", "memory-vault"]
    assert d["configured"] == ["comfyui", "n8n", "searxng"]      # every registered kind=mcp plugin's server id
    assert d["dynamic"] is True and d["ok"] is True
    assert d["registry"]["servers"]["searxng"]["url"] == "http://mcp-searxng:8080/mcp"


def test_mcp_servers_is_not_dynamic_without_the_control_plane_token():
    # the toggle persists through ops-controller; without its token nothing can be saved
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON),          patch("dashboard.app.OPS_CONTROLLER_TOKEN", ""):
        d = client.get("/api/mcp/servers").json()
    assert d["dynamic"] is False


def test_mcp_health_marks_a_server_ok_only_when_healthy_and_serving_tools():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._litellm_mcp_health",
               new=AsyncMock(return_value={"searxng": "healthy", "comfyui": "unhealthy",
                                           "memory_vault": "healthy"})), \
         patch("dashboard.app._litellm_mcp_outcomes", new=AsyncMock(return_value=(True, {
             "searxng": {"status": "ok", "tool_count": 4}, "comfyui": {"status": "unreachable"},
             "memory_vault": {"status": "ok", "tool_count": 7}}, None))):
        r = client.get("/api/mcp/health")
    d = r.json()
    assert d["ok"] is True and d["gateway"] == "reachable"
    by_id = {s["id"]: s for s in d["servers"]}
    assert by_id["searxng"] == {"id": "searxng", "ok": True, "status": "healthy", "error": None, "tool_count": 4}
    assert by_id["comfyui"]["ok"] is False and by_id["comfyui"]["status"] == "unhealthy"
    assert "unreachable" in by_id["comfyui"]["error"]
    # LiteLLM reports the hyphen-free name; the response row keeps the hyphenated server id
    assert by_id["memory-vault"] == {"id": "memory-vault", "ok": True, "status": "healthy", "error": None,
                                     "tool_count": 7}


def test_mcp_health_gateway_down_marks_everything_down():
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._litellm_mcp_health", new=AsyncMock(return_value={})), \
         patch("dashboard.app._litellm_mcp_outcomes", new=AsyncMock(return_value=(False, {}, "connect refused"))):
        d = client.get("/api/mcp/health").json()
    assert d["ok"] is False and d["gateway"] == "unreachable" and d["gateway_error"] == "connect refused"
    assert all(s["ok"] is False for s in d["servers"])


def test_mcp_add_persists_to_ordo_yaml_and_reports_render_needed(dashboard_operator_headers):
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._persist_mcp_toggle",
               new=AsyncMock(return_value={"persistent": True, "plugin": "n8n", "note": None})) as p:
        r = client.post("/api/mcp/add", json={"server": "n8n"}, headers=dashboard_operator_headers)
    d = r.json()
    p.assert_awaited_once_with("n8n", "add")
    assert d["status"] == "added" and d["applied"] is False and "model-gateway" in d["next"]
    assert d["servers"] == ["searxng", "comfyui", "memory-vault", "n8n"]


def test_mcp_add_rejects_a_server_that_is_not_a_registered_plugin(dashboard_operator_headers):
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON):
        r = client.post("/api/mcp/add", json={"server": "duckduckgo"}, headers=dashboard_operator_headers)
    assert r.status_code == 400 and "not a registered" in r.json()["detail"]


def test_mcp_remove_persists_and_reports_render_needed(dashboard_operator_headers):
    with patch("dashboard.app._read_servers_json", return_value=SERVERS_JSON), \
         patch("dashboard.app._persist_mcp_toggle",
               new=AsyncMock(return_value={"persistent": True, "plugin": "searxng", "note": None})):
        d = client.post("/api/mcp/remove", json={"server": "searxng"}, headers=dashboard_operator_headers).json()
    assert d["status"] == "removed" and d["applied"] is False and d["servers"] == ["comfyui", "memory-vault"]
