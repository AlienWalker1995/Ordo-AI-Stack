"""Dashboard /api/registry/* passthrough — unit tests."""
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from dashboard.app import app


def test_list_models_proxies_to_ops():
    with patch("dashboard.routes_registry._ops_request",
               new=AsyncMock(return_value=(200, {"models": {"local-chat": {"id": "local-chat"}}}))) as m:
        r = TestClient(app).get("/api/registry/models")
    assert r.status_code == 200
    assert "local-chat" in r.json()["models"]
    m.assert_called_once()


def test_gpus_proxies_to_ops():
    with patch("dashboard.routes_registry._ops_request",
               new=AsyncMock(return_value=(200, {"gpus": {}}))):
        r = TestClient(app).get("/api/registry/gpus")
    assert r.status_code == 200 and "gpus" in r.json()


def test_assign_gpu_409_surfaces_to_browser():
    with patch("dashboard.routes_registry._ops_request",
               new=AsyncMock(return_value=(409, {"detail": "GPU overcommitted"}))):
        r = TestClient(app).post(
            "/api/registry/models/local-chat/assign-gpu",
            json={"gpu_uuid": "GPU-20fac13a-5e5b-1818-581f-63901612fd84", "confirm": True},
        )
    assert r.status_code == 409
    assert "overcommit" in str(r.json()).lower()
