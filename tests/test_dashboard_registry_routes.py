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


def test_define_model_does_not_send_actor_field():
    """M1 regression: define_model must send json=body, not {**body, 'actor': 'dashboard'}.

    The actor is derived server-side from the X-Actor header; a dead 'actor' key
    in the body is noise that could confuse future callers.
    """
    captured = {}

    async def fake_ops_request(method, path, *, request=None, json=None, **kw):
        captured["json"] = json
        return (200, {"id": "test-model"})

    with patch("dashboard.routes_registry._ops_request", new=fake_ops_request):
        TestClient(app).post(
            "/api/registry/models",
            json={"id": "test-model", "kind": "chat", "service": "llamacpp",
                  "runtime": "single-model", "source": {}, "enabled": False, "est_vram_gb": 1.0},
        )

    assert "actor" not in (captured.get("json") or {}), (
        f"'actor' key must not appear in forwarded body; got: {captured.get('json')}"
    )
