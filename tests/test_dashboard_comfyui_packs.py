"""Test that /api/comfyui/packs exposes capability + resolved per-pack files."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """Spin up the dashboard app without hitting real ComfyUI / services."""
    # Set SCRIPTS_DIR to the repo root + scripts before importing dashboard.app
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    scripts_dir = os.path.join(repo_root, "scripts")
    monkeypatch.setenv("SCRIPTS_DIR", scripts_dir)

    import dashboard.app as dashboard_app

    # /api/comfyui/packs calls _scan_comfyui_models via asyncio.to_thread; stub the
    # sync function so the test is hermetic even without a models/ directory.
    monkeypatch.setattr(dashboard_app, "_scan_comfyui_models", lambda: [])
    return TestClient(dashboard_app.app)


def test_packs_endpoint_exposes_capability_field(client):
    """Every pack in /api/comfyui/packs must include a 'capability' string."""
    r = client.get("/api/comfyui/packs")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True, data
    assert data["packs"], "expected at least one pack from models.json"
    allowed = {"video", "image", "encoder", "upscale", "style", "other"}
    for name, pack in data["packs"].items():
        assert "capability" in pack, f"pack {name!r} missing capability"
        assert pack["capability"] in allowed, (
            f"pack {name!r} has unknown capability {pack['capability']!r}"
        )


def test_packs_endpoint_exposes_resolved_files(client):
    """Every pack must include a 'files' list of {category, name} — resolved per {quant}."""
    r = client.get("/api/comfyui/packs")
    data = r.json()
    assert data["ok"] is True, data
    for name, pack in data["packs"].items():
        assert "files" in pack, f"pack {name!r} missing files"
        assert isinstance(pack["files"], list), f"pack {name!r} files not a list"
        assert len(pack["files"]) == pack["model_count"], (
            f"pack {name!r}: files length {len(pack['files'])} != model_count {pack['model_count']}"
        )
        for f in pack["files"]:
            assert set(f.keys()) >= {"category", "name"}, f
            assert "{quant}" not in f["name"], f"quant placeholder not resolved in {name!r}: {f}"
