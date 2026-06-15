"""Tests for /registry/* HTTP endpoints in ops-controller/main.py.

Tasks 5-9: singleton + read, define/delete, assign-gpu, enable, GET /registry/gpus.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Mock docker before loading ops-controller
sys.modules.setdefault("docker", MagicMock())

_p = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_main_registry", _p)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", TOKEN)
    reg = oc.model_registry.ModelRegistry(
        registry_path=tmp_path / "reg.json",
        env_path=tmp_path / ".env",
        gpu_assignments_path=tmp_path / "gpu.yml",
    )
    monkeypatch.setattr(oc, "REGISTRY", reg)
    reg.upsert(oc.model_registry.ModelRecord(
        id="local-chat", kind="chat", service="llamacpp", runtime="single-model",
        source={"file": "q.gguf"}, gpu_uuid="GPU-abc", enabled=True, est_vram_gb=20.0,
    ))
    return TestClient(oc.app, raise_server_exceptions=False)


# ─── Task 5: singleton + read endpoints ──────────────────────────────────────

def test_list_models_requires_auth(client):
    assert client.get("/registry/models").status_code == 401


def test_list_models(client):
    r = client.get("/registry/models", headers=AUTH)
    assert r.status_code == 200 and r.json()["models"]["local-chat"]["service"] == "llamacpp"


def test_get_one_model(client):
    assert client.get("/registry/models/local-chat", headers=AUTH).json()["kind"] == "chat"
    assert client.get("/registry/models/nope", headers=AUTH).status_code == 404


# ─── Task 6: define/delete endpoints ─────────────────────────────────────────

def test_define_model(client):
    body = {
        "id": "chat-b", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {"file": "b.gguf"},
        "enabled": False, "est_vram_gb": 18.0,
    }
    r = client.post("/registry/models", json=body, headers=AUTH)
    assert r.status_code == 200
    assert oc.REGISTRY.get("chat-b") is not None


def test_define_requires_auth(client):
    body = {
        "id": "chat-c", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {}, "enabled": False, "est_vram_gb": 1.0,
    }
    assert client.post("/registry/models", json=body).status_code == 401


def test_delete_model(client):
    client.post("/registry/models", json={
        "id": "to-del", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {}, "enabled": False, "est_vram_gb": 1.0,
    }, headers=AUTH)
    r = client.delete("/registry/models/to-del", headers=AUTH)
    assert r.status_code == 200
    assert client.get("/registry/models/to-del", headers=AUTH).status_code == 404


def test_delete_missing_model_returns_404(client):
    assert client.delete("/registry/models/ghost", headers=AUTH).status_code == 404
