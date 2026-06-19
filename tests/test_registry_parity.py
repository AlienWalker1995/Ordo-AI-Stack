"""Registry parity test (Phase 4 Task 14).

Proves that a write through the Hermes actor path (X-Actor: hermes) lands in
the SAME ops-controller ModelRegistry store that the dashboard read path sees,
and that updated_by is set correctly for both actor values.
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
_spec = importlib.util.spec_from_file_location("ops_main_parity", _p)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)

TOKEN = "parity-test-token"
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
    monkeypatch.setattr(oc, "GPU_ASSIGNMENTS_PATH", tmp_path / "gpu.yml")
    reg.upsert(oc.model_registry.ModelRecord(
        id="local-chat",
        kind="chat",
        service="llamacpp",
        runtime="single-model",
        source={"file": "q.gguf"},
        gpu_uuid=_FULL_UUID,
        enabled=True,
        est_vram_gb=20.0,
    ))
    return TestClient(oc.app, raise_server_exceptions=False)


# ─── Hermes actor write lands in shared store ─────────────────────────────────

def test_hermes_assign_gpu_visible_via_shared_store(client, monkeypatch):
    """POST /registry/models/{id}/assign-gpu with X-Actor: hermes → GET sees
    the new gpu_uuid AND updated_by == 'hermes' (shared store, not a copy)."""
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: {"ok": True})
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {
                            _FULL_UUID: {"total_gb": 32.0},
                            _FULL_UUID_2: {"total_gb": 32.0},
                        })

    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": True},
        headers={**AUTH, "X-Actor": "hermes"},
    )
    assert r.status_code == 200, f"assign-gpu failed: {r.json()}"
    assert r.json()["gpu_uuid"] == _FULL_UUID_2

    # Read back through the SAME store (GET endpoint)
    get_r = client.get("/registry/models/local-chat", headers=AUTH)
    assert get_r.status_code == 200
    rec = get_r.json()
    assert rec["gpu_uuid"] == _FULL_UUID_2, "gpu_uuid not persisted in shared store"
    assert rec["updated_by"] == "hermes", (
        f"updated_by should be 'hermes', got {rec['updated_by']!r}"
    )


# ─── Dashboard actor write yields updated_by == "dashboard" ──────────────────

def test_dashboard_actor_write_sets_updated_by_dashboard(client, monkeypatch):
    """POST /registry/models/{id}/assign-gpu with no X-Actor header → updated_by=='dashboard'."""
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: {"ok": True})
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {
                            _FULL_UUID: {"total_gb": 32.0},
                            _FULL_UUID_2: {"total_gb": 32.0},
                        })

    # No X-Actor header → default "dashboard"
    r = client.post(
        "/registry/models/local-chat/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": True},
        headers=AUTH,
    )
    assert r.status_code == 200

    get_r = client.get("/registry/models/local-chat", headers=AUTH)
    rec = get_r.json()
    assert rec["updated_by"] == "dashboard", (
        f"expected updated_by='dashboard', got {rec['updated_by']!r}"
    )


# ─── Shared store: write through one client, read through another fixture ─────

def test_shared_store_parity_hermes_then_dashboard_read(monkeypatch, tmp_path):
    """Both the Hermes write path and dashboard read path see the same REGISTRY
    object — this test uses a single shared REGISTRY directly to confirm there
    is no per-request copy."""
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", TOKEN)
    reg = oc.model_registry.ModelRegistry(
        registry_path=tmp_path / "reg2.json",
        env_path=tmp_path / ".env2",
        gpu_assignments_path=tmp_path / "gpu2.yml",
    )
    monkeypatch.setattr(oc, "REGISTRY", reg)
    monkeypatch.setattr(oc, "GPU_ASSIGNMENTS_PATH", tmp_path / "gpu2.yml")
    reg.upsert(oc.model_registry.ModelRecord(
        id="model-x",
        kind="chat",
        service="llamacpp",
        runtime="single-model",
        source={"file": "x.gguf"},
        gpu_uuid=_FULL_UUID,
        enabled=True,
        est_vram_gb=10.0,
    ))

    client = TestClient(oc.app, raise_server_exceptions=False)
    monkeypatch.setattr(oc, "_recreate_service",
                        lambda svc, request=None: {"ok": True})
    monkeypatch.setattr(oc, "_live_gpus",
                        lambda: {
                            _FULL_UUID: {"total_gb": 32.0},
                            _FULL_UUID_2: {"total_gb": 32.0},
                        })

    # Hermes writes via assign-gpu
    w = client.post(
        "/registry/models/model-x/assign-gpu",
        json={"gpu_uuid": _FULL_UUID_2, "confirm": True},
        headers={**AUTH, "X-Actor": "hermes"},
    )
    assert w.status_code == 200

    # Dashboard reads via GET — same process, same REGISTRY object in memory
    get_r = client.get("/registry/models/model-x", headers=AUTH)
    assert get_r.status_code == 200
    rec = get_r.json()
    assert rec["gpu_uuid"] == _FULL_UUID_2
    assert rec["updated_by"] == "hermes"
