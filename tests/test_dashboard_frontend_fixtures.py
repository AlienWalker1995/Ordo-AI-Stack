"""The dashboard SPA's test fixtures keep the shape of what the backend actually returns.

The frontend's component tests and Playwright smoke run against canned API responses in
services/dashboard/dashboard/frontend/test/fixtures/, one JSON file per endpoint (the file's path
under fixtures/ is the URL path: api/models.json is GET /api/models). A fixture that drifts
from the backend lets the frontend suite stay green against a response the backend no longer
sends, the way MockBackend drifted from DockerBackend and shipped two 500s past a green CI.

So each fixture is compared with a real response: the real route, called through TestClient with
only its I/O (control plane, ComfyUI, Prometheus, LiteLLM, the filesystem) replaced by payloads in
the live shapes. The comparison is structural: every object has exactly the same keys, and every
value has the same JSON type (null matches anything, since most fields are nullable). Values are
free to differ; the fixtures carry a realistic stack, not these inputs.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from dashboard import routes_console
from dashboard.app import app
from ordo.control.lease_history import LeaseHistory

FIXTURES = Path(__file__).resolve().parents[1] / "services/dashboard/dashboard/frontend/test/fixtures"

GPU_FILE = "Qwen3.8-27B-TurboFCFusion-Q6_K.gguf"
CPU_FILE = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
EMBED_FILE = "nomic-embed-text-v1.5.Q4_K_M.gguf"
BIG, SMALL = "GPU-big", "GPU-small"

# What the control plane answers, in the live shapes (ordo/control/api.py).
OPS = {
    # Idle, so the model switch below is allowed.
    "/status": {"gpu": {"state": "idle", "running": [], "queued": [], "evicted_residents": {},
                        "rejected": [{"id": "train-lora", "reason": "needs 30 GB, 4 GB free"}]}},
    "/services": {"services": [
        {"id": "llamacpp", "state": "exited", "health": None, "status": "Exited (137) 1 minute ago"},
        {"id": "llamacpp-cpu", "state": "running", "health": "healthy", "status": "Up 3 hours (healthy)"},
        {"id": "open-webui", "state": "running", "health": "healthy", "status": "Up 1 hour (healthy)"},
        {"id": "evals", "state": "exited", "health": None, "status": "Exited (0) 3 hours ago"},
    ]},
    "/registry/models": {"models": {
        "local-chat": {"service": "llamacpp", "gpu_uuid": BIG, "source": {"file": GPU_FILE}},
        "comfyui": {"service": "comfyui", "gpu_uuid": BIG},
        "voice-stt": {"service": "stt", "gpu_uuid": SMALL},
    }},
    "/model-config": {"source_model": "auto", "active_model": "turbo", "active_file": GPU_FILE, "ctx_size": 106496,
                      "model_files": [{"file": GPU_FILE, "service": "llamacpp", "optional": False},
                                      {"file": CPU_FILE, "service": "llamacpp-cpu", "optional": False},
                                      {"file": EMBED_FILE, "service": "llamacpp-embed", "optional": False}],
                      "available": [{"id": "turbo", "tier": "ultra", "vram_gb": 24, "file": GPU_FILE}]},
    "/jobs/history": {"history": [{"id": "train-lora", "kind": "train", "started": 100.0, "ended": 200.0,
                                   "outcome": "completed"}]},
    "/audit?limit=100": {"entries": [{"ts": 300.0, "caller": "dashboard", "action": "restart",
                                      "target": "n8n", "result": "ok"}]},
}
HARDWARE = {"cpu_pct": 18, "ram_used_gb": 46.8, "ram_total_gb": 109.7, "ram_pct": 43,
            "disk_used_gb": 1607.1, "disk_total_gb": 1999.8, "disk_pct": 80.4,
            "gpus": [{"uuid": BIG, "name": "NVIDIA GeForce RTX 5090", "vram_total_gb": 34.2, "vram_used_gb": 29.6,
                      "utilization_pct": 97, "temp_c": 71}]}
CARDS = [
    {"id": "webui", "name": "Open WebUI", "category": "interface", "ops_service": "open-webui",
     "open_url": "https://chat.example/", "ok": True, "error": None, "background": False},
]
SERVED = {"gpu": GPU_FILE, "cpu": CPU_FILE, "embed": EMBED_FILE}
DISK = [{"name": GPU_FILE, "size": 22_400_000_000}, {"name": "spare.gguf", "size": 5_000_000_000}]
THROUGHPUT = {"models": {GPU_FILE: {"p50": 22.2}, CPU_FILE: {"p50": 2.7}}}
QUEUE = {"queue_running": [[0, "8f14e45f-ceea-467a-9575-1b0f3a1d5d2c", {}, {}, []]], "queue_pending": [[1, "p2"]]}
COMFY_HISTORY = {"r1": {"status": {"status_str": "success", "messages": [["execution_success", {"timestamp": 400000}]]},
                        "outputs": {"9": {"images": [{"filename": "ordo_00001_.png", "subfolder": "",
                                                      "type": "output"}]}}}}
PROMETHEUS_RANGE = {"status": "success", "data": {"resultType": "matrix", "result": [
    {"metric": {"job": "llamacpp"}, "values": [[1700000000, "21.5"], [1700000300, "22.9"]]},
    {"metric": {"job": "llamacpp-cpu"}, "values": [[1700000000, "2.6"], [1700000300, "2.8"]]},
]}}
STATS_SERVICES = {"gpu": {"total_gb": 34.2, "used_gb": 29.6, "utilization_pct": 97},
                  "services": {"open-webui": {"cpu_pct": 1.5, "mem_gb": 0.6, "mem_pct": 0.5, "vram_gb": 0.0,
                                              "vram_pct": 0.0, "running": True}},
                  "vram_aggregate_unavailable": False}
# out/mcp/servers.json as the render writes it (ordo/render/engine.py, the `servers.json` block).
# ControlPlane.apply_render's answer, which the switch and an MCP toggle pass on.
APPLIED = {"dry_run": False, "recreated": ["llamacpp", "llamacpp-cpu", "model-gateway"], "stopped": [],
           "restart_required_on_host": ["agent"], "host_command": "ordo apply --only agent",
           "host_reasons": {"agent": "agent runs the control plane"}}
MCP_SERVERS_JSON = {
    "servers": [{"id": "searxng", "litellm_name": "searxng", "plugin_id": "mcp-searxng", "name": "SearXNG",
                 "service": "mcp-searxng", "url": "http://mcp-searxng:8080/mcp", "network": True,
                 "tools": ["search"], "hosted": False},
                {"id": "codebase-memory", "litellm_name": "codebase_memory", "plugin_id": "mcp-codebase-memory",
                 "name": "Codebase Memory", "service": "mcp-codebase-memory",
                 "url": "http://mcp-codebase-memory:8080/mcp", "network": True, "tools": ["search_graph"],
                 "hosted": False}],
    "plugin_map": {"searxng": "mcp-searxng", "codebase-memory": "mcp-codebase-memory", "github": "mcp-github"},
}


def _get_json(url, timeout=10.0):
    if "/api/v1/query_range" in url:
        return PROMETHEUS_RANGE
    if url.endswith("/api/health"):
        return {"database": "ok", "version": "12.1.0"}
    return None


def _comfy_json(path):
    return QUEUE if path == "/queue" else COMFY_HISTORY


def _ops_controller_client(payloads: dict[str, dict]):
    """An httpx.AsyncClient stand-in answering ops-controller GETs by path suffix. One stand-in
    for both callers, because dashboard.app and routes_orchestration share the httpx module."""
    async def get(url, **_kwargs):
        response = MagicMock(status_code=200)
        response.json.return_value = next(body for suffix, body in payloads.items() if url.endswith(suffix))
        return response

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = get
    return client


def _lease_history(tmp_path) -> dict:
    """/jobs/history as the scheduler records it (ordo/control/lease_history.py), passed through
    verbatim by the dashboard's /api/orchestration/gpu/history."""
    history = LeaseHistory(tmp_path / "leases.jsonl")
    history.submitted("gate-comfyui", "media", 18.0)
    history.started("gate-comfyui")
    history.ended("gate-comfyui", "completed")
    return {"history": history.tail(100)}


@pytest.fixture
def client(tmp_path, monkeypatch, dashboard_operator_headers):
    """The dashboard app with only its I/O replaced."""
    import dashboard.app as dashboard_app

    (tmp_path / "comfy" / "checkpoints").mkdir(parents=True)
    (tmp_path / "comfy" / "checkpoints" / "sdxl.safetensors").write_bytes(b"\0" * 2048)
    monkeypatch.setattr(dashboard_app, "MODELS_DIR", tmp_path / "comfy")
    servers_json = tmp_path / "servers.json"
    servers_json.write_text(json.dumps(MCP_SERVERS_JSON), encoding="utf-8")
    monkeypatch.setattr(dashboard_app, "MCP_SERVERS_PATH", str(servers_json))

    async def ops_json(path):
        return OPS.get(path)

    async def ops_call(method, path, json=None):  # the switch's one control-plane call
        return 200, {"ok": True, "active_model": json["model"], "ctx_size": 131072, "apply": APPLIED}

    async def ops_request(method, path, **_kwargs):  # an MCP toggle's plugin enable/disable
        # A disabled plugin's container is one the render no longer defines, so the apply stops it.
        return 200, {"ok": True, "apply": {**APPLIED, "stopped": ["mcp-searxng"]}}

    with patch.object(routes_console, "_ops_json", side_effect=ops_json), \
         patch.object(routes_console, "_ops_call", side_effect=ops_call), \
         patch.object(dashboard_app, "_ops_request", side_effect=ops_request), \
         patch.object(routes_console, "_hardware", new=AsyncMock(return_value=HARDWARE)), \
         patch.object(routes_console, "_service_cards", new=AsyncMock(return_value=CARDS)), \
         patch.object(routes_console, "_rag", new=AsyncMock(return_value={"ok": True, "points_count": 2483})), \
         patch.object(routes_console, "_throughput", new=AsyncMock(return_value=THROUGHPUT)), \
         patch.object(routes_console, "_served", new=AsyncMock(return_value=SERVED)), \
         patch.object(routes_console, "_disk_files", new=AsyncMock(return_value=DISK)), \
         patch.object(routes_console, "_comfy_json", new=AsyncMock(side_effect=_comfy_json)), \
         patch.object(routes_console, "_get_json", new=AsyncMock(side_effect=_get_json)), \
         patch.object(dashboard_app, "_litellm_mcp_health", new=AsyncMock(return_value={"searxng": "healthy"})), \
         patch.object(dashboard_app, "_litellm_mcp_outcomes", new=AsyncMock(
             return_value=(True, {"searxng": {"status": "ok", "tool_count": 1}}, None))), \
         patch("httpx.AsyncClient", return_value=_ops_controller_client(
             {"/stats/services": STATS_SERVICES, "/jobs/history": _lease_history(tmp_path)})):
        yield TestClient(app, headers=dashboard_operator_headers)


def _fixture_files() -> list[str]:
    return sorted(p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*.json"))


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


# Objects keyed by an id rather than records with fixed fields: their keys are data, so each
# value is compared with the backend's values instead of key by key.
KEYED_BY_ID = frozenset({"api/mcp/servers.json.registry.servers"})


def _shape_mismatches(fixture, real, where: str) -> list[str]:
    """Every place `fixture` is not shaped like `real`; empty when they match."""
    if fixture is None or real is None:
        return []
    fixture_type, real_type = _type_name(fixture), _type_name(real)
    if fixture_type != real_type:
        return [f"{where}: fixture has {fixture_type}, the backend returns {real_type}"]
    if fixture_type == "object" and where in KEYED_BY_ID:
        return _shape_mismatches(list(fixture.values()), list(real.values()), f"{where}[*]")
    if fixture_type == "object":
        problems = []
        missing = sorted(set(real) - set(fixture))
        extra = sorted(set(fixture) - set(real))
        if missing:
            problems.append(f"{where}: fixture lacks keys the backend returns: {missing}")
        if extra:
            problems.append(f"{where}: fixture has keys the backend does not return: {extra}")
        for key in sorted(set(fixture) & set(real)):
            problems += _shape_mismatches(fixture[key], real[key], f"{where}.{key}")
        return problems
    if fixture_type == "array":
        if fixture and not real:
            return [f"{where}: the backend returned no elements to compare the fixture's against; "
                    "give its inputs one"]
        problems = []
        for i, element in enumerate(fixture):
            # A list may mix element shapes (a probe-only row beside a container row): an element
            # matches when it is shaped like any one the backend returned.
            candidates = [_shape_mismatches(element, r, f"{where}[{i}]") for r in real]
            if all(candidates):
                problems += min(candidates, key=len)
        return problems
    return []


# Each fixture file and the real request that produces the same response: (method, path, body).
REAL_REQUESTS = {
    "api/overview.json": ("GET", "/api/overview", None),
    "api/activity.json": ("GET", "/api/activity", None),
    "api/services/table.json": ("GET", "/api/services/table", None),
    "api/hardware/service-pressure.json": ("GET", "/api/hardware/service-pressure", None),
    "api/models.json": ("GET", "/api/models", None),
    "api/models/switch.json": ("POST", "/api/models/switch", {"model": "turbo"}),
    "api/media.json": ("GET", "/api/media", None),
    "api/comfyui/models.json": ("GET", "/api/comfyui/models", None),
    "api/orchestration/gpu/history.json": ("GET", "/api/orchestration/gpu/history", None),
    "api/perf/series.json": ("GET", "/api/perf/series", None),
    "api/perf/grafana.json": ("GET", "/api/perf/grafana", None),
    "api/auth/session.json": ("GET", "/api/auth/session", None),
    "api/mcp/servers.json": ("GET", "/api/mcp/servers", None),
    "api/mcp/health.json": ("GET", "/api/mcp/health", None),
    "api/mcp/remove.json": ("POST", "/api/mcp/remove", {"server": "searxng"}),
}


def test_every_fixture_is_checked_against_a_real_endpoint():
    # A fixture added without a row here would never be compared, and could drift unseen.
    assert _fixture_files() == sorted(REAL_REQUESTS)


@pytest.mark.parametrize("fixture_file", sorted(REAL_REQUESTS))
def test_fixture_matches_the_backend_response_shape(client, fixture_file):
    method, path, body = REAL_REQUESTS[fixture_file]
    response = client.request(method, path, json=body)
    assert response.status_code == 200, response.text
    fixture = json.loads((FIXTURES / fixture_file).read_text(encoding="utf-8"))
    assert _shape_mismatches(fixture, response.json(), fixture_file) == []


def test_the_shape_check_catches_a_renamed_key_and_a_changed_type():
    real = {"gpus": [{"name": "RTX 5090", "vram_used_gb": 29.6}], "ok": True}
    assert _shape_mismatches({"gpus": [{"name": "x", "vram_used_gb": 1}], "ok": False}, real, "r") == []
    renamed = {"gpus": [{"name": "x", "vram_gb": 1}], "ok": False}
    assert _shape_mismatches(renamed, real, "r") == [
        "r.gpus[0]: fixture lacks keys the backend returns: ['vram_used_gb']",
        "r.gpus[0]: fixture has keys the backend does not return: ['vram_gb']",
    ]
    assert _shape_mismatches({"gpus": [], "ok": "yes"}, real, "r") == [
        "r.ok: fixture has string, the backend returns boolean"]
