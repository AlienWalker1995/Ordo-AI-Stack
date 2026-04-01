"""
E2E orchestration pipeline test.

Path: template compile → worker run → artifact receipt → publish enqueue →
      publish callback → state persists after simulated restart.

No GPU required: uses mock_comfyui fixture.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def mock_comfyui_url():
    """Start mock ComfyUI server for the test module."""
    from tests.fixtures.mock_comfyui import start_mock_comfyui
    start_mock_comfyui(host="127.0.0.1", port=18188)
    return "http://127.0.0.1:18188"


@pytest.fixture
def db_dir(tmp_path: Path):
    return tmp_path / "dashboard"


@pytest.fixture
def client(db_dir: Path, mock_comfyui_url: str, monkeypatch, tmp_path):
    """Dashboard TestClient with isolated DB and pointing at mock ComfyUI."""
    monkeypatch.setenv("DASHBOARD_DATA_PATH", str(db_dir))
    monkeypatch.setenv("COMFYUI_URL", mock_comfyui_url)

    # Patch readiness so it passes without live services
    with patch("dashboard.routes_orchestration.compute_readiness",
               return_value={"ok": True, "checks": []}):
        from dashboard.orchestration_db import init_db, load_store
        init_db(db_dir)
        load_store(db_dir)

        # Re-import app after env setup
        import importlib
        import dashboard.routes_orchestration as ro
        importlib.reload(ro)

        from dashboard.app import app
        yield TestClient(app)


def _poll_job(client: TestClient, job_id: str, target_state: str, timeout: int = 15) -> dict:
    """Poll job status until target_state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/orchestration/jobs/{job_id}")
        assert r.status_code == 200
        j = r.json()
        if j["state"] == target_state:
            return j
        if j["state"] == "failed":
            pytest.fail(f"Job {job_id} failed: {j.get('error')}")
        time.sleep(0.2)
    pytest.fail(f"Job {job_id} did not reach {target_state!r} within {timeout}s; last state={j['state']!r}")


def test_full_pipeline_compile_run_artifact_publish_callback(
    client: TestClient, db_dir: Path, mock_comfyui_url: str, tmp_path: Path
):
    """Full pipeline: template compile → run (via worker thread) → artifact → publish callback → published."""
    from dashboard.orchestration_db import (
        claim_next_job, get_job, load_store, recover_stale_running_jobs,
    )
    from tests.fixtures.mock_comfyui import start_mock_comfyui

    # Step 1: create a minimal workflow file for this test
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True)
    wf_file = wf_dir / "test_social.json"
    wf_file.write_text(json.dumps({
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "PARAM_STR_prompt", "clip": ["2", 0]}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "test.safetensors"}},
    }), encoding="utf-8")

    with patch("dashboard.routes_orchestration.WORKFLOWS_DIR", wf_dir), \
         patch("dashboard.routes_orchestration.compute_readiness", return_value={"ok": True, "checks": []}):

        # Step 2: queue the job via API
        r = client.post("/api/orchestration/run", json={
            "workflow_id": "test_social",
            "params": {"prompt": "a robot dancing"},
        })
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        assert r.json()["state"] == "queued"

        # Step 3: run the worker inline (simulates the worker container)
        load_store(db_dir)
        job = claim_next_job(db_dir)
        assert job is not None
        assert job.job_id == job_id

        from worker.worker import execute_job as _execute_job
        import os
        os.environ["COMFYUI_URL"] = mock_comfyui_url
        os.environ["COMFYUI_WORKFLOWS_DIR"] = str(wf_dir)

        import worker.worker as ww
        ww.DATA_DIR = db_dir
        ww.COMFYUI_URL = mock_comfyui_url
        ww.WORKFLOWS_DIR = wf_dir
        _execute_job(job)

        # Step 4: assert artifact_ready
        j = get_job(db_dir, job_id)
        assert j is not None
        assert j.state.value == "artifact_ready", f"Expected artifact_ready, got {j.state}"
        assert j.outputs is not None

        # Step 5: publish enqueue → writes to outbox (no live HTTP)
        r2 = client.post("/api/orchestration/publish/enqueue", json={
            "job_id": job_id,
            "webhook_url": "http://n8n:5678/webhook/test",
            "payload": {"platform": "test"},
        })
        assert r2.status_code == 200
        idem_key = r2.json()["idempotency_key"]

        j2 = get_job(db_dir, job_id)
        assert j2.state.value == "publish_enqueued"

        # Step 6: n8n calls publish callback
        r3 = client.post("/api/orchestration/publish/callback", json={
            "job_id": job_id,
            "status": "delivered",
            "idempotency_key": idem_key,
        })
        assert r3.status_code == 200

        j3 = get_job(db_dir, job_id)
        assert j3.state.value == "published"

        # Step 7: simulate worker crash and restart — state must persist
        recover_stale_running_jobs(db_dir)  # would reset 'running' jobs; published should be untouched
        j4 = get_job(db_dir, job_id)
        assert j4.state.value == "published", "State must survive simulated worker restart"


def test_cancel_queued_job(client: TestClient, db_dir: Path):
    """Cancellation of a queued job before execution."""
    from dashboard.orchestration_db import get_job, load_store

    with patch("dashboard.routes_orchestration.compute_readiness", return_value={"ok": True}):
        r = client.post("/api/orchestration/run", json={"workflow_id": "test", "params": {}})
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        r2 = client.post(f"/api/orchestration/jobs/{job_id}/cancel")
        assert r2.status_code == 200

        load_store(db_dir)
        j = get_job(db_dir, job_id)
        assert j.state.value == "cancelling"


def test_stale_running_job_recovery(db_dir: Path):
    """Jobs stuck in 'running' at startup must be re-queued."""
    from dashboard.orchestration_db import (
        JobState, create_job, get_job, init_db, recover_stale_running_jobs, update_job,
    )
    init_db(db_dir)
    job = create_job(db_dir, workflow_id="test", params={})
    update_job(db_dir, job.job_id, state=JobState.running)

    recovered = recover_stale_running_jobs(db_dir)
    assert recovered >= 1
    j = get_job(db_dir, job.job_id)
    assert j.state.value == "queued"


def test_workflow_version_lifecycle(client: TestClient, db_dir: Path):
    """Save → promote → diff → rollback workflow lifecycle."""
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


def test_schedule_crud(client: TestClient, db_dir: Path):
    """Create, list, update, delete a schedule."""
    r = client.post("/api/orchestration/schedules", json={
        "cron_expr": "0 9 * * *",
        "workflow_id": "social-bot",
        "params": {"prompt": "trending topic"},
    })
    assert r.status_code == 200
    sid = r.json()["schedule_id"]
    assert r.json()["enabled"] == 1

    r2 = client.patch(f"/api/orchestration/schedules/{sid}", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["enabled"] == 0

    r3 = client.get("/api/orchestration/schedules")
    assert r3.status_code == 200
    assert any(s["schedule_id"] == sid for s in r3.json()["schedules"])

    r4 = client.delete(f"/api/orchestration/schedules/{sid}")
    assert r4.status_code == 200
