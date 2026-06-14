"""ComfyUI restart hardening (#3):

- ops-controller `/services/{id}/restart` debounces rapid repeat calls so an
  agent retry-loop "storm" collapses into a single in-flight restart.
- dashboard `/api/orchestration/comfyui/status` is a ComfyUI-INDEPENDENT health
  verb (queries ops-controller, not ComfyUI) so agents stop guessing raw
  `/api/comfyui/*` paths.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# ── ops-controller restart debounce ──────────────────────────────────────────

sys.modules.setdefault("docker", MagicMock())
_OPS_PATH = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_controller_main_harden", _OPS_PATH)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)

VALID_TOKEN = "test-secret-token"
_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture()
def ops_client(monkeypatch):
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", VALID_TOKEN)
    oc._restart_state.clear()
    return TestClient(oc.app, raise_server_exceptions=False)


def test_rapid_restarts_collapse_to_one(ops_client, monkeypatch):
    restarts: list[int] = []
    fake = MagicMock()
    fake.restart = MagicMock(side_effect=lambda timeout=30: restarts.append(1))
    monkeypatch.setattr(oc, "_containers_for_service", lambda sid: [fake])

    r1 = ops_client.post("/services/comfyui/restart", json={"confirm": True}, headers=_AUTH)
    r2 = ops_client.post("/services/comfyui/restart", json={"confirm": True}, headers=_AUTH)
    r3 = ops_client.post("/services/comfyui/restart", json={"confirm": True}, headers=_AUTH)

    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json()["action"] == "restarted"
    assert r2.json()["action"] == "debounced"
    assert r3.json()["action"] == "debounced"
    # The storm issued ONE real docker restart, not three.
    assert len(restarts) == 1


def test_debounce_disabled_when_zero(ops_client, monkeypatch):
    monkeypatch.setattr(oc, "RESTART_DEBOUNCE_SECONDS", 0.0)
    monkeypatch.setattr(oc, "_containers_for_service", lambda sid: [MagicMock()])

    r1 = ops_client.post("/services/comfyui/restart", json={"confirm": True}, headers=_AUTH)
    r2 = ops_client.post("/services/comfyui/restart", json={"confirm": True}, headers=_AUTH)
    assert r1.json()["action"] == "restarted"
    assert r2.json()["action"] == "restarted"  # no debounce window


# ── dashboard comfyui_status (ComfyUI-independent) ────────────────────────────


@pytest.fixture()
def dash_client():
    from dashboard.app import app
    return TestClient(app, raise_server_exceptions=False)


def test_status_503_without_ops_token(dash_client, monkeypatch):
    import dashboard.routes_orchestration as ro
    monkeypatch.setattr(ro, "OPS_CONTROLLER_TOKEN", "")
    r = dash_client.get("/api/orchestration/comfyui/status")
    assert r.status_code == 503


def test_status_aggregates_ops_without_touching_comfyui(dash_client, monkeypatch):
    import dashboard.routes_orchestration as ro
    monkeypatch.setattr(ro, "OPS_CONTROLLER_TOKEN", "tok")

    class _Resp:
        def __init__(self, data: dict[str, Any]):
            self.status_code = 200
            self._d = data

        def json(self) -> dict[str, Any]:
            return self._d

    class _AsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            if url.endswith("/services"):
                return _Resp({"services": [{"id": "comfyui", "state": "running"}]})
            if url.endswith("/guardian/status"):
                return _Resp({"comfyui_queue": {"running": 0, "pending": 0, "reachable": True}})
            return _Resp({})

    monkeypatch.setattr(ro.httpx, "AsyncClient", _AsyncClient)

    r = dash_client.get("/api/orchestration/comfyui/status")
    assert r.status_code == 200
    body = r.json()
    assert body["container_state"] == "running"
    assert body["queue"]["reachable"] is True
    assert body["up"] is True
