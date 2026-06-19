"""Tests for /registry/* HTTP endpoints in ops-controller/main.py.

Tasks 5-9: singleton + read, define/delete, assign-gpu, enable, GET /registry/gpus.
"""
from __future__ import annotations

import importlib.util
import os
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

_FULL_UUID = "GPU-12345678-1234-1234-1234-123456789abc"
_FULL_UUID_2 = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", TOKEN)
    reg = oc.model_registry.ModelRegistry(
        registry_path=tmp_path / "reg.json",
        env_path=tmp_path / ".env",
        gpu_assignments_path=tmp_path / "gpu.yml",
    )
    monkeypatch.setattr(oc, "REGISTRY", reg)
    # endpoint writes via the module-level GPU_ASSIGNMENTS_PATH (prod: same path as
    # the registry's); point it at the tmp file so the test is hermetic, not /workspace
    monkeypatch.setattr(oc, "GPU_ASSIGNMENTS_PATH", tmp_path / "gpu.yml")
    reg.upsert(oc.model_registry.ModelRecord(
        id="local-chat", kind="chat", service="llamacpp", runtime="single-model",
        source={"file": "q.gguf"}, gpu_uuid=_FULL_UUID, enabled=True, est_vram_gb=20.0,
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

def test_assign_gpu_sets_pin_and_recreates(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: calls.setdefault("svc", svc) or {"ok": True})
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID_2: {"total_gb": 32.0}, _FULL_UUID: {"total_gb": 32.0}})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["gpu_uuid"] == _FULL_UUID_2
    assert calls["svc"] == "llamacpp" and oc.REGISTRY.get("local-chat").gpu_uuid == _FULL_UUID_2


def test_assign_gpu_rejects_bad_uuid(client):
    # "nope" is clearly invalid
    assert client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "nope", "confirm": True},
        headers=AUTH,
    ).status_code == 400
    # FIX 2 regression: short-form "GPU-abc" must also be rejected by the strict regex
    assert client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": "GPU-abc", "confirm": True},
        headers=AUTH,
    ).status_code == 400


def test_assign_gpu_requires_confirm(client):
    assert client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": False},
        headers=AUTH,
    ).status_code == 400


def test_assign_gpu_missing_model_returns_404(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID_2: {"total_gb": 32.0}})
    assert client.post(
        "/registry/models/ghost/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": True},
        headers=AUTH,
    ).status_code == 404


def test_assign_gpu_capacity_check_blocks_overcommit(client, monkeypatch):
    # _FULL_UUID only has 8 GB; local-chat needs 20 GB => should fail
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID: {"total_gb": 8.0}})
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID, "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 409


def test_assign_gpu_force_bypasses_capacity(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID: {"total_gb": 8.0}})
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: calls.setdefault("svc", svc) or {"ok": True})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID, "confirm": True, "force": True},
        headers=AUTH,
    )
    assert r.status_code == 200


def test_assign_gpu_to_same_gpu_not_double_counted(client, monkeypatch):
    """FIX 4 regression: reassigning a model to its current GPU must not
    double-count the model's own VRAM and spuriously reject the operation."""
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    # local-chat: est_vram_gb=20, assigned to _FULL_UUID (total 32 GB)
    # Without self-exclusion: 20 (existing) + 20 (candidate) = 40 > 32 → 409
    # With self-exclusion: 0 (others on this GPU) + 20 (candidate) = 20 <= 32 → 200
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID: {"total_gb": 32.0}})
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID, "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.json()}"


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


def test_enable_rolls_back_on_recreate_failure(client, monkeypatch):
    """FIX 3 regression: if _recreate_service raises, enabled states are restored."""
    monkeypatch.setattr(oc, "_set_env_keys", lambda kv, request=None: None)
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: (_ for _ in ()).throw(RuntimeError("boom")))
    # Add a second model to enable (local-chat is currently enabled)
    client.post("/registry/models", json={
        "id": "chat-b", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {"file": "b.gguf"},
        "enabled": False, "est_vram_gb": 18.0,
    }, headers=AUTH)
    r = client.post("/registry/models/chat-b/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 500
    # chat-b must be rolled back to disabled
    assert oc.REGISTRY.get("chat-b").enabled is False
    # local-chat must be restored to enabled
    assert oc.REGISTRY.get("local-chat").enabled is True


def test_enable_rejects_newline_in_model_file(client, monkeypatch):
    """FIX 6 regression: a model whose source.file contains a newline is rejected at env-write."""
    # _recreate_service is a no-op so the test reaches _set_env_keys
    monkeypatch.setattr(oc, "_recreate_service", lambda svc, request=None: {"ok": True})
    client.post("/registry/models", json={
        "id": "evil", "kind": "chat", "service": "llamacpp",
        "runtime": "single-model", "source": {"file": "a.gguf\nEVIL=1"},
        "enabled": False, "est_vram_gb": 1.0,
    }, headers=AUTH)
    r = client.post("/registry/models/evil/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.json()}"
    # The registry must NOT have been left in a corrupted enabled state
    assert oc.REGISTRY.get("evil").enabled is False


# ─── Task 9: GET /registry/gpus ───────────────────────────────────────────────

def test_registry_gpus_lists_assignments(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {_FULL_UUID: {"name": "5090", "total_gb": 32.0,
                                              "used_gb": 20.0, "util": 5}})
    g = client.get("/registry/gpus", headers=AUTH).json()["gpus"][_FULL_UUID]
    assert "local-chat" in g["models"] and g["total_gb"] == 32.0


def test_registry_gpus_requires_auth(client):
    assert client.get("/registry/gpus").status_code == 401


def test_registry_gpus_empty_when_no_gpus(client, monkeypatch):
    monkeypatch.setattr(oc, "_live_gpus", lambda: {})
    r = client.get("/registry/gpus", headers=AUTH)
    assert r.status_code == 200 and r.json()["gpus"] == {}


# ─── Task 15b: reconcile-on-startup ───────────────────────────────────────────

def test_reconcile_registry_on_startup_seeds_from_env(monkeypatch, tmp_path):
    """_reconcile_registry_on_startup() seeds local-chat from .env LLAMACPP_MODEL."""
    (tmp_path / ".env").write_text("LLAMACPP_MODEL=qwen.gguf\n", encoding="utf-8")
    reg = oc.model_registry.ModelRegistry(
        registry_path=tmp_path / "reg.json",
        env_path=tmp_path / ".env",
        gpu_assignments_path=tmp_path / "gpu.yml",
    )
    monkeypatch.setattr(oc, "REGISTRY", reg)
    oc._reconcile_registry_on_startup()
    assert oc.REGISTRY.get("local-chat").source["file"] == "qwen.gguf"


# ─── C1 regression: env_path must default to /workspace/.env ─────────────────

def test_registry_env_path_defaults_to_container_workspace(monkeypatch):
    """When OPS_ENV_PATH is unset the REGISTRY env_path must be /workspace/.env.

    The module-level REGISTRY was constructed at import time. In the test suite
    OPS_ENV_PATH is typically unset (CI/dev) so REGISTRY.env_path already reflects
    the default. We also construct a fresh instance with the same default expression
    to prove the fix is robust even when the monkeypatched env is cleared.
    """
    monkeypatch.delenv("OPS_ENV_PATH", raising=False)
    # Assert module-level REGISTRY (built at import) uses the container path.
    assert oc.REGISTRY.env_path.as_posix() == "/workspace/.env", (
        f"REGISTRY.env_path is {oc.REGISTRY.env_path.as_posix()!r}; "
        "expected '/workspace/.env'. Was BASE_PATH used instead of the container path?"
    )
    # Also verify a freshly constructed registry with the same default expression.
    fresh = oc.model_registry.ModelRegistry(
        registry_path=Path("/data/model-registry.json"),
        env_path=Path(os.environ.get("OPS_ENV_PATH", "/workspace/.env")),
        gpu_assignments_path=oc.GPU_ASSIGNMENTS_PATH,
    )
    assert fresh.env_path.as_posix() == "/workspace/.env"


# ─── I1 regression: env rollback when no sibling was previously active ────────

def test_enable_restores_prior_env_on_recreate_failure(monkeypatch, tmp_path):
    """If _recreate_service fails, the .env must be restored to its prior value.

    This test covers the case where NO sibling was previously active — the old
    rollback only restored env via prev_active_record, so when nothing was active
    the .env was left pointing at the NEW model after a recreate failure.
    """
    # Seed a writable temp .env with the original model value.
    env_file = tmp_path / ".env"
    env_file.write_text("LLAMACPP_MODEL=old.gguf\n", encoding="utf-8")

    # Registry with no initially-enabled records (clean slate, prev_active_record=None).
    reg = oc.model_registry.ModelRegistry(
        registry_path=tmp_path / "reg.json",
        env_path=env_file,
        gpu_assignments_path=tmp_path / "gpu.yml",
    )
    # Define local-chat as DISABLED so there is no prev_active_record when we enable chat-b.
    reg.upsert(oc.model_registry.ModelRecord(
        id="local-chat", kind="chat", service="llamacpp", runtime="single-model",
        source={"file": "old.gguf"}, enabled=False, est_vram_gb=0.0,
    ))
    # Define chat-b (to be enabled).
    reg.upsert(oc.model_registry.ModelRecord(
        id="chat-b", kind="chat", service="llamacpp", runtime="single-model",
        source={"file": "new.gguf"}, enabled=False, est_vram_gb=18.0,
    ))

    monkeypatch.setattr(oc, "REGISTRY", reg)
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", TOKEN)
    # Let _set_env_keys run for real (it writes to tmp .env via REGISTRY.env_path).
    # Only _recreate_service is monkeypatched to simulate a compose failure.
    monkeypatch.setattr(
        oc, "_recreate_service",
        lambda svc, request=None: (_ for _ in ()).throw(RuntimeError("compose boom")),
    )

    c = TestClient(oc.app, raise_server_exceptions=False)
    r = c.post("/registry/models/chat-b/enable", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 500, f"Expected 500, got {r.status_code}: {r.json()}"

    # The .env must be restored to the original value.
    restored = oc.model_registry._parse_env(env_file)
    assert restored.get("LLAMACPP_MODEL") == "old.gguf", (
        f"After recreate failure .env has LLAMACPP_MODEL={restored.get('LLAMACPP_MODEL')!r}; "
        "expected 'old.gguf'. The env rollback did not restore the prior value."
    )
    # Registry state must also be rolled back.
    assert reg.get("chat-b").enabled is False
