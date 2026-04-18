"""Tests for /api/hardware/service-pressure proxy."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

def _mk_httpx_response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r

def _mk_async_client(response):
    ac = MagicMock()
    ac.__aenter__ = AsyncMock(return_value=ac)
    ac.__aexit__ = AsyncMock(return_value=None)
    ac.get = AsyncMock(return_value=response)
    return ac

def test_service_pressure_no_auth_required(monkeypatch):
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": {"total_gb": 24.0, "used_gb": 6.0, "utilization_pct": 30},
        "services": {"comfyui": {"cpu_pct": 10.0, "mem_gb": 1.0, "mem_pct": 2.0, "vram_gb": 6.0, "vram_pct": 25.0, "running": True}},
        "vram_aggregate_unavailable": False,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        r = TestClient(app.app).get("/api/hardware/service-pressure")
    assert r.status_code == 200
    d = r.json()
    ids = {s["id"] for s in d["services"]}
    assert {"llamacpp", "comfyui", "webui", "openclaw", "qdrant", "n8n", "mcp", "model-gateway"} <= ids
    assert d["services"][0]["id"] == "comfyui"
    assert d["services"][0]["has_gpu"] is True
    assert d["services"][0]["vram_gb"] == 6.0

def test_service_pressure_sorts_by_max_percent(monkeypatch):
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": None,
        "services": {
            "open-webui": {"cpu_pct": 80.0, "mem_gb": 0.5, "mem_pct": 1.0, "vram_gb": 0.0, "vram_pct": 0.0, "running": True},
            "comfyui": {"cpu_pct": 5.0, "mem_gb": 1.0, "mem_pct": 2.0, "vram_gb": 0.5, "vram_pct": 2.0, "running": True},
        },
        "vram_aggregate_unavailable": True,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert d["services"][0]["id"] == "webui"
    assert d["services"][0]["cpu_pct"] == 80.0

def test_service_pressure_maps_compose_name_to_display_id():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({
        "gpu": None,
        "services": {"open-webui": {"cpu_pct": 1.0, "mem_gb": 0.1, "mem_pct": 0.2, "vram_gb": 0.0, "vram_pct": 0.0, "running": True}},
        "vram_aggregate_unavailable": False,
    }))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    webui_row = next(s for s in d["services"] if s["id"] == "webui")
    assert webui_row["name"] == "Open WebUI"
    assert webui_row["running"] is True

def test_service_pressure_missing_services_filled_as_zero():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({"gpu": None, "services": {}, "vram_aggregate_unavailable": True}))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert len(d["services"]) >= 8
    assert all(not s["running"] for s in d["services"])

def test_service_pressure_ops_controller_unreachable(monkeypatch):
    import dashboard.app as app
    ac = MagicMock()
    ac.__aenteer__ = AsyncMock(return_value=ac)
    ac.__aexit__ = AsyncMock(return_value=None)
    ac.get = AsyncMock(side_effect=app._httpx.ConnectError("unreachable"))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        r = TestClient(app.app).get("/api/hardware/service-pressure")
    assert r.status_code == 200
    d = r.json()
    assert d["vram_aggregate_unavailable"] is True
    assert all(not s["running"] for s in d["services"])

def test_service_pressure_ops_controller_error_status():
    import dashboard.app as app
    ac = _mk_async_client(_mk_httpx_response({}, status=500))
    with patch("dashboard.app._httpx.AsyncClient", return_value=ac):
        d = TestClient(app.app).get("/api/hardware/service-pressure").json()
    assert d["vram_aggregate_unavailable"] is True
    assert all(not s["running"] for s in d["services"])