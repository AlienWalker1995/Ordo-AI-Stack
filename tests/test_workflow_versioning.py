"""Tests for workflow versioning and rollback endpoints in routes_orchestration.py."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# --- A minimal valid API-format workflow for ComfyUI ---

def _make_workflow(text: str = "hello") -> dict:
    return {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": text, "clip": ["2", 0]},
        },
    }


# --- Fixtures ---

@pytest.fixture
def db_dir(tmp_path: Path) -> Path:
    return tmp_path / "dashboard"


@pytest.fixture
def client(db_dir: Path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_PATH", str(db_dir))

    with patch(
        "dashboard.routes_orchestration.compute_readiness",
        return_value={"ok": True, "checks": []},
    ):
        from dashboard.orchestration_db import init_db, load_store

        init_db(db_dir)
        load_store(db_dir)

        import dashboard.routes_orchestration as ro

        importlib.reload(ro)

        from dashboard.app import app

        yield TestClient(app)


# --- Save + list ---

def test_save_and_list_versions(client: TestClient):
    """Save a workflow version, list versions, verify it appears."""
    wf = _make_workflow("v1")
    r = client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-alpha", "workflow": wf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["workflow_id"] == "wf-alpha"
    assert data["version"] == 1

    # List versions
    r2 = client.get("/api/orchestration/workflows/wf-alpha/versions")
    assert r2.status_code == 200
    versions = r2.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["version"] == 1


def test_save_increments_version(client: TestClient):
    """Saving multiple times increments the version number."""
    for i in range(1, 4):
        r = client.post(
            "/api/orchestration/workflows/save",
            json={"workflow_id": "wf-inc", "workflow": _make_workflow(f"v{i}")},
        )
        assert r.status_code == 200
        assert r.json()["version"] == i

    r2 = client.get("/api/orchestration/workflows/wf-inc/versions")
    assert len(r2.json()["versions"]) == 3


# --- Diff ---

def test_diff_two_versions(client: TestClient):
    """Save two versions, diff them, verify diff output."""
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-diff", "workflow": _make_workflow("version-one")},
    )
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-diff", "workflow": _make_workflow("version-two")},
    )

    r = client.post("/api/orchestration/workflows/wf-diff/diff?v1=1&v2=2")
    assert r.status_code == 200
    body = r.json()
    assert body["workflow_id"] == "wf-diff"
    assert body["v1"] == 1
    assert body["v2"] == 2
    # Unified diff should contain both old and new text
    assert "version-one" in body["diff"]
    assert "version-two" in body["diff"]


def test_diff_identical_versions(client: TestClient):
    """Diffing a version against itself produces an empty diff."""
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-same", "workflow": _make_workflow("same")},
    )
    r = client.post("/api/orchestration/workflows/wf-same/diff?v1=1&v2=1")
    assert r.status_code == 200
    assert r.json()["diff"] == ""


def _load_orchestration_mcp_server(monkeypatch):
    """Import services/orchestration/server.py with a stand-in FastMCP.

    mcp/fastmcp is a service dependency, not a CI test dependency, so the stand-in keeps
    this test running in CI. Its tool() decorator returns the function unchanged, exactly
    like the real one, so the test calls the tool function the MCP server exposes.
    """

    class _StandInFastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            return lambda fn: fn

    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _StandInFastMCP
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    server_path = Path(__file__).resolve().parents[1] / "services" / "orchestration" / "server.py"
    spec = importlib.util.spec_from_file_location("orchestration_mcp_server_under_test", server_path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def test_mcp_diff_tool_request_reaches_the_diff_route(client: TestClient, monkeypatch):
    """The diff_workflow_versions MCP tool's request is accepted by the dashboard route.

    The tool must send v1/v2 the way the route reads them (query params); a body-only
    request is rejected with 422 before the handler runs.
    """
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-mcp-diff", "workflow": _make_workflow("mcp-one")},
    )
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-mcp-diff", "workflow": _make_workflow("mcp-two")},
    )
    server = _load_orchestration_mcp_server(monkeypatch)

    class _DashboardClient:
        """Stands in for httpx.Client(timeout=...) and sends every request to the test app."""

        def __init__(self, timeout: float):
            pass

        def __enter__(self):
            return client

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(server, "httpx", types.SimpleNamespace(Client=_DashboardClient))

    body = server.diff_workflow_versions("wf-mcp-diff", 1, 2)

    assert body["v1"] == 1
    assert body["v2"] == 2
    assert "mcp-one" in body["diff"]
    assert "mcp-two" in body["diff"]


# --- Promote ---

def test_promote_version(client: TestClient):
    """Promote a version and verify success."""
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-promo", "workflow": _make_workflow("v1")},
    )
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-promo", "workflow": _make_workflow("v2")},
    )

    r = client.post("/api/orchestration/workflows/wf-promo/promote?version=2")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["promoted_version"] == 2

    # Verify promoted_at is set on the version
    r2 = client.get("/api/orchestration/workflows/wf-promo/versions/2")
    assert r2.status_code == 200
    assert r2.json()["promoted_at"] is not None


# --- Rollback ---

def test_rollback_creates_new_version(client: TestClient):
    """Rollback to a previous version creates a new version with the old content."""
    wf1 = _make_workflow("original")
    wf2 = _make_workflow("updated")

    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-rb", "workflow": wf1},
    )
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-rb", "workflow": wf2},
    )

    r = client.post("/api/orchestration/workflows/wf-rb/rollback?to_version=1")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["new_version"] == 3
    assert body["rolled_back_to"] == 1

    # Verify the new version has the original workflow content
    r2 = client.get("/api/orchestration/workflows/wf-rb/versions/3")
    assert r2.status_code == 200
    v3 = r2.json()
    assert v3["compiled_json"]["1"]["inputs"]["text"] == "original"
    assert v3["rollback_of"] == 1

    # Verify total version count
    r3 = client.get("/api/orchestration/workflows/wf-rb/versions")
    assert len(r3.json()["versions"]) == 3


# --- 404 cases ---

def test_diff_nonexistent_version_returns_404(client: TestClient):
    """Diff with non-existent versions returns 404."""
    # No versions saved for this workflow
    r = client.post("/api/orchestration/workflows/wf-ghost/diff?v1=1&v2=2")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_diff_one_version_missing_returns_404(client: TestClient):
    """Diff where one version exists but the other does not returns 404."""
    client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-half", "workflow": _make_workflow("v1")},
    )
    r = client.post("/api/orchestration/workflows/wf-half/diff?v1=1&v2=99")
    assert r.status_code == 404


def test_promote_nonexistent_version_returns_404(client: TestClient):
    """Promoting a non-existent version returns 404."""
    r = client.post("/api/orchestration/workflows/wf-nope/promote?version=42")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_rollback_nonexistent_version_returns_404(client: TestClient):
    """Rolling back to a non-existent version returns 404."""
    r = client.post("/api/orchestration/workflows/wf-nope/rollback?to_version=99")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_save_invalid_workflow_returns_400(client: TestClient):
    """Saving an invalid (UI-format) workflow returns 400."""
    ui_workflow = {"nodes": [{"type": "SomeNode"}]}
    r = client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "wf-bad", "workflow": ui_workflow},
    )
    assert r.status_code == 400


def test_save_empty_workflow_id_returns_400(client: TestClient):
    """Saving with an empty workflow_id returns 400."""
    r = client.post(
        "/api/orchestration/workflows/save",
        json={"workflow_id": "", "workflow": _make_workflow()},
    )
    assert r.status_code == 400
