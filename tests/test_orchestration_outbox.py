"""Tests for publish callback endpoint and outbox retry logic.

Covers:
- POST /api/orchestration/publish/callback happy path, missing key, non-existent job
- Outbox DB functions: create, retrieve pending, mark delivered, record attempt, backoff
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db_dir(tmp_path: Path):
    return tmp_path / "dashboard"


@pytest.fixture
def client(db_dir: Path, monkeypatch):
    """Dashboard TestClient with isolated DB; readiness mocked."""
    monkeypatch.setenv("DASHBOARD_DATA_PATH", str(db_dir))

    with patch(
        "dashboard.routes_orchestration.compute_readiness",
        return_value={"ok": True, "checks": []},
    ):
        from dashboard.orchestration_db import init_db, load_store

        init_db(db_dir)
        load_store(db_dir)

        import importlib

        import dashboard.routes_orchestration as ro

        importlib.reload(ro)

        from dashboard.app import app

        yield TestClient(app)


def _create_job(db_dir: Path, **kwargs) -> str:
    """Helper: create a job and return its job_id."""
    from dashboard.orchestration_db import create_job

    job = create_job(db_dir, workflow_id="test-wf", params={}, **kwargs)
    return job.job_id


def _advance_job_to_artifact_ready(db_dir: Path, job_id: str) -> None:
    """Walk a queued job through the valid state machine to artifact_ready."""
    from dashboard.orchestration_db import JobState, update_job

    for state in (JobState.validated, JobState.running, JobState.artifact_ready):
        update_job(db_dir, job_id, state=state)


# ── Publish callback endpoint tests ──────────────────────────────────────────


class TestPublishCallback:
    """POST /api/orchestration/publish/callback"""

    def test_happy_path_delivered(self, client: TestClient, db_dir: Path):
        """Valid callback with matching idempotency_key transitions job to published."""
        from dashboard.orchestration_db import (
            JobState,
            create_outbox_entry,
            get_job,
            get_pending_outbox,
            update_job,
        )

        job_id = _create_job(db_dir)
        _advance_job_to_artifact_ready(db_dir, job_id)
        update_job(db_dir, job_id, state=JobState.publish_enqueued)

        webhook = "http://n8n:5678/webhook/test"
        idem_key = create_outbox_entry(
            db_dir, job_id, webhook, {"job_id": job_id, "payload": {}}
        )

        r = client.post(
            "/api/orchestration/publish/callback",
            json={
                "job_id": job_id,
                "status": "delivered",
                "idempotency_key": idem_key,
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

        j = get_job(db_dir, job_id)
        assert j.state == JobState.published
        assert j.publish_status == "published"

        # Outbox entry should be marked delivered
        pending = get_pending_outbox(db_dir)
        assert all(e["idempotency_key"] != idem_key for e in pending)

    def test_callback_failed_status(self, client: TestClient, db_dir: Path):
        """Failed callback updates publish_status but does not change state to published."""
        from dashboard.orchestration_db import JobState, get_job, update_job

        job_id = _create_job(db_dir)
        _advance_job_to_artifact_ready(db_dir, job_id)
        update_job(db_dir, job_id, state=JobState.publish_enqueued)

        r = client.post(
            "/api/orchestration/publish/callback",
            json={
                "job_id": job_id,
                "status": "failed",
                "error": "rate limited",
            },
        )
        assert r.status_code == 200

        j = get_job(db_dir, job_id)
        # State should NOT be published
        assert j.state == JobState.publish_enqueued
        assert "rate limited" in j.publish_status

    def test_callback_without_idempotency_key(self, client: TestClient, db_dir: Path):
        """Delivered callback without idempotency_key still updates job state."""
        from dashboard.orchestration_db import JobState, get_job, update_job

        job_id = _create_job(db_dir)
        _advance_job_to_artifact_ready(db_dir, job_id)
        update_job(db_dir, job_id, state=JobState.publish_enqueued)

        r = client.post(
            "/api/orchestration/publish/callback",
            json={
                "job_id": job_id,
                "status": "delivered",
                # no idempotency_key
            },
        )
        assert r.status_code == 200

        j = get_job(db_dir, job_id)
        assert j.state == JobState.published

    def test_callback_nonexistent_job(self, client: TestClient):
        """Callback for a non-existent job returns 404."""
        r = client.post(
            "/api/orchestration/publish/callback",
            json={
                "job_id": "does-not-exist",
                "status": "delivered",
            },
        )
        assert r.status_code == 404
        assert "Unknown job_id" in r.json()["detail"]


# ── Outbox DB function tests ─────────────────────────────────────────────────


class TestOutboxDB:
    """Direct tests for orchestration_db outbox functions."""

    def test_create_and_retrieve_pending(self, db_dir: Path):
        """Creating an outbox entry makes it appear in get_pending_outbox."""
        from dashboard.orchestration_db import (
            create_outbox_entry,
            get_pending_outbox,
            init_db,
        )

        init_db(db_dir)
        job_id = _create_job(db_dir)
        webhook = "http://example.com/hook"
        payload = {"job_id": job_id, "data": "test"}

        key = create_outbox_entry(db_dir, job_id, webhook, payload)
        assert isinstance(key, str)
        assert len(key) > 0

        pending = get_pending_outbox(db_dir)
        assert len(pending) == 1
        assert pending[0]["job_id"] == job_id
        assert pending[0]["webhook_url"] == webhook
        assert pending[0]["idempotency_key"] == key
        assert pending[0]["attempts"] == 0
        assert pending[0]["delivered_at"] is None

    def test_idempotency_key_is_deterministic(self, db_dir: Path):
        """Same job_id + webhook_url produces the same idempotency_key."""
        from dashboard.orchestration_db import create_outbox_entry, init_db

        init_db(db_dir)
        job_id = _create_job(db_dir)
        webhook = "http://example.com/hook"

        key1 = create_outbox_entry(db_dir, job_id, webhook, {"a": 1})
        key2 = create_outbox_entry(db_dir, job_id, webhook, {"b": 2})
        assert key1 == key2

    def test_mark_outbox_delivered(self, db_dir: Path):
        """Marking an entry as delivered removes it from pending results."""
        from dashboard.orchestration_db import (
            create_outbox_entry,
            get_pending_outbox,
            init_db,
            mark_outbox_delivered,
        )

        init_db(db_dir)
        job_id = _create_job(db_dir)
        key = create_outbox_entry(
            db_dir, job_id, "http://example.com/hook", {"job_id": job_id}
        )

        mark_outbox_delivered(db_dir, key)

        pending = get_pending_outbox(db_dir)
        assert len(pending) == 0

    def test_record_attempt_increments_count(self, db_dir: Path):
        """Each call to record_outbox_attempt increments the attempts counter."""
        import sqlite3

        from dashboard.orchestration_db import (
            create_outbox_entry,
            init_db,
            record_outbox_attempt,
        )

        init_db(db_dir)
        job_id = _create_job(db_dir)
        create_outbox_entry(
            db_dir, job_id, "http://example.com/hook", {"job_id": job_id}
        )

        db_path = db_dir / "orchestration" / "orchestration.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            "SELECT id FROM publish_outbox WHERE job_id=?", (job_id,)
        ).fetchone()
        row_id = row["id"]

        record_outbox_attempt(db_dir, row_id, error="timeout")
        entry = conn.execute(
            "SELECT attempts, error FROM publish_outbox WHERE id=?", (row_id,)
        ).fetchone()
        assert entry["attempts"] == 1
        assert entry["error"] == "timeout"

        record_outbox_attempt(db_dir, row_id, error="connection refused")
        entry = conn.execute(
            "SELECT attempts, error FROM publish_outbox WHERE id=?", (row_id,)
        ).fetchone()
        assert entry["attempts"] == 2
        assert entry["error"] == "connection refused"

        conn.close()

    def test_exponential_backoff_delays(self, db_dir: Path):
        """Backoff increases: 30s, 120s, 480s, 1920s, capped at 7200s."""
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from dashboard.orchestration_db import (
            create_outbox_entry,
            init_db,
            record_outbox_attempt,
        )

        init_db(db_dir)
        job_id = _create_job(db_dir)
        create_outbox_entry(
            db_dir, job_id, "http://example.com/hook", {"job_id": job_id}
        )

        # Get the row_id
        db_path = db_dir / "orchestration" / "orchestration.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id FROM publish_outbox WHERE job_id=?", (job_id,)
        ).fetchone()
        row_id = row["id"]

        expected_delays = [30, 120, 480, 1920, 7200]
        for i, expected_delay in enumerate(expected_delays):
            before = datetime.now(UTC)
            record_outbox_attempt(db_dir, row_id, error=f"attempt {i + 1}")

            result = conn.execute(
                "SELECT next_retry_at, attempts FROM publish_outbox WHERE id=?",
                (row_id,),
            ).fetchone()

            assert result["attempts"] == i + 1

            # Parse next_retry_at and verify it is approximately correct
            next_retry = datetime.fromisoformat(
                result["next_retry_at"].replace("Z", "+00:00")
            )
            expected_earliest = before + timedelta(seconds=expected_delay - 2)
            expected_latest = before + timedelta(seconds=expected_delay + 2)
            assert expected_earliest <= next_retry <= expected_latest, (
                f"Attempt {i + 1}: expected delay ~{expected_delay}s, "
                f"got next_retry_at={result['next_retry_at']}"
            )

        conn.close()

    def test_max_attempts_excludes_from_pending(self, db_dir: Path):
        """Entries with attempts >= max_attempts are not returned by get_pending_outbox."""
        import sqlite3

        from dashboard.orchestration_db import (
            create_outbox_entry,
            get_pending_outbox,
            init_db,
            record_outbox_attempt,
        )

        init_db(db_dir)
        job_id = _create_job(db_dir)
        create_outbox_entry(
            db_dir, job_id, "http://example.com/hook", {"job_id": job_id}
        )

        pending = get_pending_outbox(db_dir)
        row_id = pending[0]["id"]

        # Record 5 attempts (default max_attempts)
        for _ in range(5):
            record_outbox_attempt(db_dir, row_id, error="fail")

        # Reset next_retry_at to the past so the backoff filter doesn't hide it
        db_path = db_dir / "orchestration" / "orchestration.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE publish_outbox SET next_retry_at='2000-01-01T00:00:00Z' WHERE id=?",
            (row_id,),
        )
        conn.commit()
        conn.close()

        # Should not appear with default max_attempts=5
        pending = get_pending_outbox(db_dir)
        assert len(pending) == 0

        # Should appear with higher max_attempts
        pending = get_pending_outbox(db_dir, max_attempts=10)
        assert len(pending) == 1
