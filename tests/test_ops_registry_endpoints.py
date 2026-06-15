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


# ─── Task 7: assign-gpu endpoint ─────────────────────────────────────────────

_FULL_UUID = "GPU-12345678-1234-1234-1234-123456789abc"


def test_assign_gpu_sets_pin_and_recreates(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: calls.setdefault("svc", svc) or {"ok": True})
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {"GPU-def": {"total_gb": 32.0}, "GPU-abc": {"total_gb": 32.0}})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "GPU-def", "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["gpu_uuid"] == "GPU-def"
    assert calls["svc"] == "llamacpp" and oc.REGISTRY.get("local-chat").gpu_uuid == "GPU-def"


def test_assign_gpu_rejects_bad_uuid(client):
    assert client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "nope", "confirm": True},
        headers=AUTH,
    ).status_code == 400


def test_assign_gpu_requires_confirm(client):
    assert client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "GPU-def", "confirm": False},
        headers=AUTH,
    ).status_code == 400


def test_assign_gpu_missing_model_returns_404(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {"GPU-def": {"total_gb": 32.0}})
    assert client.post(
        "/registry/models/ghost/assign-gpu",
        json={"gpu_uuid": "GPU-def", "confirm": True},
        headers=AUTH,
    ).status_code == 404


def test_assign_gpu_capacity_check_blocks_overcommit(client, monkeypatch):
    # GPU-abc only has 8 GB; local-chat needs 20 GB => should fail
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {"GPU-abc": {"total_gb": 8.0}})
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "GPU-abc", "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 409


def test_assign_gpu_force_bypasses_capacity(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {"GPU-abc": {"total_gb": 8.0}})
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: calls.setdefault("svc", svc) or {"ok": True})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "GPU-abc", "confirm": True, "force": True},
        headers=AUTH,
    )
    assert r.status_code == 200


# ─── Task 8: enable endpoint ──────────────────────────────────────────────────

def test_enable_swaps_active_and_writes_env(client, monkeypatch):
    written = {}
    monkeypatch.setattr(oc, "_set_env_keys", lambda kv, request=None: written.update(kv))
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    client.post("/registry/models", json={
        "id": "chat-b", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {"file": "b.gguf"},
        "config": {"ctx": 65536},
        "enabled": False, "est_vram_gb": 18.0,
    }, headers=AUTH)
    r = client.post("/registry/models/chat-b/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200
    assert written["LLAMACPP_MODEL"] == "b.gguf" and written["LLAMACPP_CTX_SIZE"] == "65536"
    assert oc.REGISTRY.get("chat-b").enabled is True
    assert oc.REGISTRY.get("local-chat").enabled is False


def test_enable_requires_confirm(client, monkeypatch):
    monkeypatch.setattr(oc, "_set_env_keys", lambda kv, request=None: None)
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    r = client.post("/registry/models/local-chat/enable", json={"confirm": False}, headers=AUTH)
    assert r.status_code == 400


def test_enable_missing_model_returns_404(client):
    r = client.post("/registry/models/ghost/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 404


def test_enable_rejects_non_single_model(client, monkeypatch):
    monkeypatch.setattr(oc, "_set_env_keys", lambda kv, request=None: None)
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    client.post("/registry/models", json={
        "id": "comfy", "kind": "comfyui", "service": "comfyui",
        "runtime": "multi-model", "source": {}, "enabled": False, "est_vram_gb": 0.0,
    }, headers=AUTH)
    r = client.post("/registry/models/comfy/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400


# ─── Task 9: GET /registry/gpus ───────────────────────────────────────────────

def test_registry_gpus_lists_assignments(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {"GPU-abc": {"name": "5090", "total_gb": 32.0,
                                             "used_gb": 20.0, "util": 5}})
    g = client.get("/registry/gpus", headers=AUTH).json()["gpus"]["GPU-abc"]
    assert "local-chat" in g["models"] and g["total_gb"] == 32.0


def test_registry_gpus_requires_auth(client):
    assert client.get("/registry/gpus").status_code == 401


def test_registry_gpus_empty_when_no_gpus(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus", lambda: {})
    r = client.get("/registry/gpus", headers=AUTH)
    assert r.status_code == 200 and r.json()["gpus"] == {}
