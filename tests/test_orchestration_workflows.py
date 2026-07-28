"""Workflow-lifecycle coverage carried forward from the retired worker e2e/outbox suites.

The media "worker" (job/publish/schedule queue) was retired — see
tests/test_service_catalog_wiring.py and services/v1-parity/dashboard/routes_orchestration.py
(the /run, /jobs*, /publish/*, /schedules* verbs now 410). This file preserves the parts of
the deleted tests/test_orchestration_e2e.py and tests/test_orchestration_outbox.py that
exercised still-LIVE surface:

  - POST /api/orchestration/workflows/save + /promote + /rollback + /versions (worker-independent
    workflow version lifecycle; from test_orchestration_e2e.py::test_workflow_version_lifecycle).
  - _resolve_workflow_under_root path-traversal guard, which still backs /validate and the
    workflow lifecycle routes (from test_orchestration_e2e.py::test_safe_workflow_path_rejects_traversal).
  - The DELETE-journal-mode invariant on orchestration.sqlite3 (from
    test_orchestration_outbox.py::TestDeleteJournalInvariant). The jobs/outbox tables are dead with
    the worker, but the SAME db file + `_connect` path is still written on every live
    /workflows/save call, so the "no WAL -shm on the 9p mount" lesson still applies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def db_dir(tmp_path: Path):
    return tmp_path / "dashboard"


@pytest.fixture
def client(db_dir: Path, monkeypatch):
    """Dashboard TestClient with isolated DB."""
    monkeypatch.setenv("DASHBOARD_DATA_PATH", str(db_dir))

    from dashboard.orchestration_db import init_db, load_store

    init_db(db_dir)
    load_store(db_dir)

    import importlib

    import dashboard.routes_orchestration as ro

    importlib.reload(ro)

    from dashboard.app import app

    yield TestClient(app)


def test_workflow_version_lifecycle(client: TestClient, db_dir: Path):
    """Save -> promote -> diff -> rollback workflow lifecycle (worker-independent)."""
    wf = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "v1", "clip": ["2", 0]}}}

    r = client.post("/api/orchestration/workflows/save",
                    json={"workflow_id": "test-wf", "workflow": wf})
    assert r.status_code == 200
    assert r.json()["version"] == 1

    wf2 = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "v2", "clip": ["2", 0]}}}
    r2 = client.post("/api/orchestration/workflows/save",
                     json={"workflow_id": "test-wf", "workflow": wf2})
    assert r2.status_code == 200
    assert r2.json()["version"] == 2

    r3 = client.post("/api/orchestration/workflows/test-wf/promote?version=2")
    assert r3.status_code == 200

    r4 = client.post("/api/orchestration/workflows/test-wf/rollback?to_version=1")
    assert r4.status_code == 200
    assert r4.json()["new_version"] == 3

    r5 = client.get("/api/orchestration/workflows/test-wf/versions")
    assert r5.status_code == 200
    assert len(r5.json()["versions"]) == 3


def test_safe_workflow_path_rejects_traversal():
    """_resolve_workflow_under_root must reject path traversal attempts.

    Still backs _safe_workflow_path, used by the live POST /api/orchestration/validate route.
    """
    from dashboard.routes_orchestration import _resolve_workflow_under_root

    root = Path(__file__).parent
    # Basic traversal
    assert _resolve_workflow_under_root("../../etc/passwd", root) is None
    # Backslash normalization
    assert _resolve_workflow_under_root("..\\..\\etc\\passwd", root) is None
    # Absolute path
    assert _resolve_workflow_under_root("/etc/passwd", root) is None
    # Empty
    assert _resolve_workflow_under_root("", root) is None
    assert _resolve_workflow_under_root("   ", root) is None
    # Dot-dot in middle
    assert _resolve_workflow_under_root("sub/../../../etc/passwd", root) is None


class TestDeleteJournalInvariant:
    """The orchestration DB must use DELETE journal mode, never WAL.

    WAL's -shm mmap crash-loops the control plane on the 9p bind mount; this test guards
    that invariant. Exercised via save_workflow_version — the write path /workflows/save
    actually uses in production now that the job/outbox tables are dead with the worker.
    """

    def test_journal_mode_is_delete_and_no_wal_sidecar(self, db_dir: Path):
        import sqlite3

        from dashboard.orchestration_db import _connect, init_db, save_workflow_version

        init_db(db_dir)
        # Perform a real write through the production connection path.
        version = save_workflow_version(db_dir, "journal-check-wf", {"1": {"class_type": "X", "inputs": {}}})
        assert version == 1

        db_path = db_dir / "orchestration" / "orchestration.sqlite3"
        assert db_path.is_file()

        # The production connection path must report DELETE journal mode.
        with _connect(db_dir) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "delete"

        # A raw connection (no PRAGMA override) sees the persisted mode too — proving
        # the DB was never switched to WAL on disk.
        raw = sqlite3.connect(str(db_path))
        try:
            persisted = raw.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            raw.close()
        assert str(persisted).lower() == "delete"

        # No WAL/-shm sidecar files may exist next to the DB.
        assert not (db_path.parent / "orchestration.sqlite3-wal").exists()
        assert not (db_path.parent / "orchestration.sqlite3-shm").exists()
