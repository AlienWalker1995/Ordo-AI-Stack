"""Dashboard /api/registry/* passthrough — unit tests."""
from fastapi.testclient import TestClient
from unittest.mock import patch

from dashboard.app import app


def test_list_models_proxies_to_ops():
    with patch("dashboard.routes_registry._ops_request",
               return_value=(200, {"models": {"local-chat": {"id": "local-chat"}}})) as m:
        r = TestClient(app).get("/api/registry/models")
    assert r.status_code == 200
    assert "local-chat" in r.json()["models"]
    m.assert_called_once()


def test_gpus_proxies_to_ops():
    with patch("dashboard.routes_registry._ops_request",
               return_value=(200, {"gpus": {}})):
        r = TestClient(app).get("/api/registry/gpus")
    assert r.status_code == 200 and "gpus" in r.json()
