"""GET /api/dependencies is now derived from the single services_catalog.

The old parallel catalog (dependency_registry.py + dependency_registry.json) was
consolidated into services_catalog. These tests lock:
  * the module + JSON are gone and nothing imports them;
  * the dependency view is manifest-gated and appends core infra;
  * headless (check-less) services never produce a false-red row;
  * the response shape the dashboard's dependency panel reads is preserved.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.services_catalog import (  # noqa: E402
    INFRA_DEPENDENCIES,
    dependency_services,
    probe_all,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"

# Fields the frontend (static/index.html loadDependencies) reads off each entry.
FRONTEND_ENTRY_FIELDS = {"id", "name", "category", "ok", "latency_ms", "error", "hint"}


def test_dependency_registry_module_and_json_deleted():
    assert not (DASHBOARD / "dependency_registry.py").exists()
    assert not (DASHBOARD / "dependency_registry.json").exists()


def test_no_source_imports_dependency_registry():
    # Only flag actual import statements — a prose mention in a docstring/comment
    # documenting the consolidation is fine.
    hits = []
    for p in DASHBOARD.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if "dependency_registry" in line and "import" in line:
                hits.append(f"{p}: {line.strip()}")
    assert not hits, f"dependency_registry still imported in: {hits}"


def test_infra_dependencies_present_and_shaped():
    ids = {e["id"] for e in INFRA_DEPENDENCIES}
    assert ids == {"ops-controller", "dashboard"}
    for e in INFRA_DEPENDENCIES:
        assert {"id", "name", "category", "check", "hint"} <= set(e)


def test_dependency_services_manifest_gated_and_appends_infra(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"plugins_enabled": ["open-webui", "comfyui"]}), encoding="utf-8")
    monkeypatch.setenv("MANIFEST_PATH", str(manifest))

    ids = [e["id"] for e in dependency_services()]
    # Core services (plugin=None) with a check always present.
    assert {"llamacpp", "model-gateway", "mcp"} <= set(ids)
    # Enabled-plugin services present.
    assert {"webui", "comfyui"} <= set(ids)
    # Disabled-plugin services absent.
    for hidden in ("qdrant", "n8n", "hermes", "stt", "tts"):
        assert hidden not in ids
    # Core infra always appended.
    assert {"ops-controller", "dashboard"} <= set(ids)


def test_headless_services_excluded_even_when_enabled(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"plugins_enabled": ["worker", "rag"]}), encoding="utf-8")
    monkeypatch.setenv("MANIFEST_PATH", str(manifest))

    ids = [e["id"] for e in dependency_services()]
    # worker + rag-ingestion expose no `check` -> excluded (no false-red row).
    assert "worker" not in ids
    assert "rag-ingestion" not in ids
    # qdrant (rag plugin, has a check) IS included.
    assert "qdrant" in ids


def test_probe_all_shape_and_fields(monkeypatch):
    # Empty MANIFEST_PATH -> fail open -> full catalog.
    monkeypatch.setenv("MANIFEST_PATH", "")
    resp = MagicMock(status_code=200)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    out = asyncio.run(probe_all(client))
    assert {"version", "description", "entries"} <= set(out)
    assert out["version"] == 1
    assert isinstance(out["entries"], list) and out["entries"]
    for e in out["entries"]:
        assert FRONTEND_ENTRY_FIELDS <= set(e)
    ids = {e["id"] for e in out["entries"]}
    # Infra always probed; headless workers never appear.
    assert {"ops-controller", "dashboard"} <= ids
    assert "worker" not in ids and "rag-ingestion" not in ids
