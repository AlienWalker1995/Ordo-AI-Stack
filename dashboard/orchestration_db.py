"""SQLite-backed job store, publish outbox, workflow versions, and schedules.

Replaces the in-memory + single-JSON approach in orchestration_jobs.py.
WAL mode allows concurrent dashboard readers + single worker writer safely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobState(StrEnum):
    queued = "queued"
    validated = "validated"
    running = "running"
    artifact_ready = "artifact_ready"
    publish_enqueued = "publish_enqueued"
    published = "published"
    failed = "failed"
    cancelling = "cancelling"
    cancelled = "cancelled"


@dataclass
class OrchestrationJob:
    job_id: str
    state: JobState
    created_at: str
    updated_at: str
    template_id: str | None = None
    workflow_id: str | None = None
    prompt_id: str | None = None
    error: str | None = None
    outputs: dict[str, Any] | None = None
    publish_webhook: str | None = None
    publish_status: str | None = None
    params_json: str | None = None
    compiled_workflow: str | None = None
    retry_count: int = 0
    scheduled_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _db_path(data_dir: Path) -> Path:
    d = data_dir / "orchestration"
    d.mkdir(parents=True, exist_ok=True)
    return d / "orchestration.db"


def _connect(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(data_dir)), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    template_id TEXT,
    workflow_id TEXT,
    prompt_id TEXT,
    error TEXT,
    outputs_json TEXT,
    publish_webhook TEXT,
    publish_status TEXT,
    params_json TEXT,
    compiled_workflow TEXT,
    retry_count INTEGER DEFAULT 0,
    scheduled_at TEXT,
    extra_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS publish_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    next_retry_at TEXT,
    delivered_at TEXT,
    error TEXT,
    idempotency_key TEXT UNIQUE,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    compiled_json TEXT NOT NULL,
    params_schema TEXT,
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    rollback_of INTEGER,
    UNIQUE(workflow_id, version)
);

CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    cron_expr TEXT NOT NULL,
    template_id TEXT,
    workflow_id TEXT,
    params_json TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL
);
"""


def init_db(data_dir: Path) -> None:
    """Create tables and migrate legacy JSON store if present."""
    with _connect(data_dir) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    _migrate_json_store(data_dir)


def _migrate_json_store(data_dir: Path) -> None:
    legacy = data_dir / "orchestration" / "orchestration_jobs.json"
    if not legacy.is_file():
        return
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception:
        return
    with _connect(data_dir) as conn:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if count > 0:
            return  # already migrated
        for jid, row in raw.items():
            try:
                st = JobState(row.get("state", "queued"))
            except ValueError:
                st = JobState.queued
            conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (job_id, state, created_at, updated_at, template_id, workflow_id,
                    prompt_id, error, outputs_json, publish_webhook, publish_status, extra_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    jid, st.value,
                    row.get("created_at", _now_iso()),
                    row.get("updated_at", _now_iso()),
                    row.get("template_id"),
                    row.get("workflow_id"),
                    row.get("prompt_id"),
                    row.get("error"),
                    json.dumps(row.get("outputs")) if row.get("outputs") else None,
                    row.get("publish_webhook"),
                    row.get("publish_status"),
                    json.dumps(row.get("extra") or {}),
                ),
            )
        conn.commit()


def _row_to_job(row: sqlite3.Row) -> OrchestrationJob:
    try:
        state = JobState(row["state"])
    except ValueError:
        state = JobState.queued
    outputs = None
    if row["outputs_json"]:
        try:
            outputs = json.loads(row["outputs_json"])
        except Exception:
            pass
    extra: dict = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            pass
    return OrchestrationJob(
        job_id=row["job_id"],
        state=state,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        template_id=row["template_id"],
        workflow_id=row["workflow_id"],
        prompt_id=row["prompt_id"],
        error=row["error"],
        outputs=outputs,
        publish_webhook=row["publish_webhook"],
        publish_status=row["publish_status"],
        params_json=row["params_json"],
        compiled_workflow=row["compiled_workflow"],
        retry_count=row["retry_count"] or 0,
        scheduled_at=row["scheduled_at"],
        extra=extra,
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────

def create_job(
    data_dir: Path,
    *,
    template_id: str | None = None,
    workflow_id: str | None = None,
    params: dict[str, Any] | None = None,
    compiled_workflow: dict[str, Any] | None = None,
    scheduled_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> OrchestrationJob:
    jid = str(uuid.uuid4())
    t = _now_iso()
    with _connect(data_dir) as conn:
        conn.execute(
            """INSERT INTO jobs
               (job_id, state, created_at, updated_at, template_id, workflow_id,
                params_json, compiled_workflow, scheduled_at, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                jid, JobState.queued.value, t, t,
                template_id, workflow_id,
                json.dumps(params) if params else None,
                json.dumps(compiled_workflow) if compiled_workflow else None,
                scheduled_at,
                json.dumps(extra or {}),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
    return _row_to_job(row)


def get_job(data_dir: Path, job_id: str) -> OrchestrationJob | None:
    with _connect(data_dir) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(data_dir: Path, state: str | None = None, limit: int = 100) -> list[OrchestrationJob]:
    with _connect(data_dir) as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_job(r) for r in rows]


def update_job(data_dir: Path, job_id: str, **fields: Any) -> OrchestrationJob | None:
    if not fields:
        return get_job(data_dir, job_id)
    allowed = {
        "state", "prompt_id", "error", "outputs", "publish_webhook",
        "publish_status", "retry_count", "compiled_workflow", "params_json",
    }
    col_map = {"outputs": "outputs_json", "state": "state"}
    sets = ["updated_at=?"]
    vals: list[Any] = [_now_iso()]
    for k, v in fields.items():
        if k not in allowed:
            continue
        col = col_map.get(k, k)
        if k == "outputs":
            v = json.dumps(v) if v is not None else None
        elif k == "state" and isinstance(v, JobState):
            v = v.value
        sets.append(f"{col}=?")
        vals.append(v)
    if len(sets) == 1:
        return get_job(data_dir, job_id)
    vals.append(job_id)
    with _connect(data_dir) as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def claim_next_job(data_dir: Path) -> OrchestrationJob | None:
    """Atomically claim one queued job → validated. Returns None if queue empty."""
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE state=? ORDER BY created_at ASC LIMIT 1",
            (JobState.queued.value,),
        ).fetchone()
        if not row:
            return None
        jid = row["job_id"]
        now = _now_iso()
        updated = conn.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state=?",
            (JobState.validated.value, now, jid, JobState.queued.value),
        ).rowcount
        conn.commit()
        if updated == 0:
            return None  # lost the race
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
    return _row_to_job(row) if row else None


def cancel_job(data_dir: Path, job_id: str) -> OrchestrationJob | None:
    """Request cancellation for a queued/validated job."""
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state IN (?,?)",
            (JobState.cancelling.value, _now_iso(), job_id,
             JobState.queued.value, JobState.validated.value),
        )
        conn.commit()
    return get_job(data_dir, job_id)


def recover_stale_running_jobs(data_dir: Path) -> int:
    """On worker startup: re-queue any jobs stuck in running/validated (from a previous crash)."""
    now = _now_iso()
    with _connect(data_dir) as conn:
        result = conn.execute(
            "UPDATE jobs SET state=?, updated_at=?, error='recovered from stale running state' "
            "WHERE state IN (?,?)",
            (JobState.queued.value, now, JobState.running.value, JobState.validated.value),
        )
        conn.commit()
        return result.rowcount


# ── Publish outbox ─────────────────────────────────────────────────────────────

def _outbox_key(job_id: str, webhook_url: str) -> str:
    return hashlib.sha256(f"{job_id}:{webhook_url}".encode()).hexdigest()


def create_outbox_entry(
    data_dir: Path,
    job_id: str,
    webhook_url: str,
    payload: dict[str, Any],
) -> str:
    key = _outbox_key(job_id, webhook_url)
    with _connect(data_dir) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO publish_outbox
               (job_id, webhook_url, payload_json, idempotency_key)
               VALUES (?,?,?,?)""",
            (job_id, webhook_url, json.dumps(payload), key),
        )
        conn.commit()
    return key


def get_pending_outbox(data_dir: Path, max_attempts: int = 5) -> list[dict[str, Any]]:
    now = _now_iso()
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """SELECT * FROM publish_outbox
               WHERE delivered_at IS NULL
               AND attempts < ?
               AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY id ASC LIMIT 20""",
            (max_attempts, now),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_outbox_delivered(data_dir: Path, idempotency_key: str) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE publish_outbox SET delivered_at=?, error=NULL WHERE idempotency_key=?",
            (_now_iso(), idempotency_key),
        )
        conn.commit()


def record_outbox_attempt(data_dir: Path, row_id: int, error: str | None = None) -> None:
    import math
    with _connect(data_dir) as conn:
        attempts = conn.execute(
            "SELECT attempts FROM publish_outbox WHERE id=?", (row_id,)
        ).fetchone()
        n = (attempts["attempts"] or 0) + 1
        # Exponential backoff: 30s, 2m, 8m, 30m, 2h
        delay_sec = min(30 * (4 ** (n - 1)), 7200)
        from datetime import timedelta
        next_retry = (datetime.now(UTC) + timedelta(seconds=delay_sec)).isoformat().replace("+00:00", "Z")
        conn.execute(
            "UPDATE publish_outbox SET attempts=?, last_attempt_at=?, next_retry_at=?, error=? WHERE id=?",
            (n, _now_iso(), next_retry, error, row_id),
        )
        conn.commit()


# ── Workflow versions ──────────────────────────────────────────────────────────

def save_workflow_version(
    data_dir: Path,
    workflow_id: str,
    compiled_json: dict[str, Any],
    params_schema: dict[str, Any] | None = None,
) -> int:
    """Save a new version; returns the version number."""
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT MAX(version) as mv FROM workflow_versions WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        next_v = (row["mv"] or 0) + 1
        conn.execute(
            """INSERT INTO workflow_versions
               (workflow_id, version, compiled_json, params_schema, created_at)
               VALUES (?,?,?,?,?)""",
            (
                workflow_id, next_v,
                json.dumps(compiled_json),
                json.dumps(params_schema) if params_schema else None,
                _now_iso(),
            ),
        )
        conn.commit()
    return next_v


def list_workflow_versions(data_dir: Path, workflow_id: str) -> list[dict[str, Any]]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT id, workflow_id, version, params_schema, created_at, promoted_at, rollback_of "
            "FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC",
            (workflow_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_workflow_version(
    data_dir: Path, workflow_id: str, version: int
) -> dict[str, Any] | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?",
            (workflow_id, version),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["compiled_json"] = json.loads(d["compiled_json"])
    except Exception:
        pass
    return d


def promote_workflow_version(data_dir: Path, workflow_id: str, version: int) -> bool:
    with _connect(data_dir) as conn:
        result = conn.execute(
            "UPDATE workflow_versions SET promoted_at=? WHERE workflow_id=? AND version=?",
            (_now_iso(), workflow_id, version),
        )
        conn.commit()
    return result.rowcount > 0


def get_promoted_workflow(data_dir: Path, workflow_id: str) -> dict[str, Any] | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id=? AND promoted_at IS NOT NULL "
            "ORDER BY version DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["compiled_json"] = json.loads(d["compiled_json"])
    except Exception:
        pass
    return d


def rollback_workflow(data_dir: Path, workflow_id: str, to_version: int) -> int | None:
    """Create a new version that is a copy of to_version; returns new version number."""
    src = get_workflow_version(data_dir, workflow_id, to_version)
    if not src:
        return None
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT MAX(version) as mv FROM workflow_versions WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        next_v = (row["mv"] or 0) + 1
        conn.execute(
            """INSERT INTO workflow_versions
               (workflow_id, version, compiled_json, params_schema, created_at, rollback_of)
               VALUES (?,?,?,?,?,?)""",
            (
                workflow_id, next_v,
                json.dumps(src["compiled_json"]) if isinstance(src["compiled_json"], dict)
                else src["compiled_json"],
                src.get("params_schema"),
                _now_iso(),
                src["id"],
            ),
        )
        conn.commit()
    return next_v


# ── Schedules ─────────────────────────────────────────────────────────────────

def create_schedule(
    data_dir: Path,
    cron_expr: str,
    template_id: str | None = None,
    workflow_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = str(uuid.uuid4())
    now = _now_iso()
    try:
        from croniter import croniter
        next_run = croniter(cron_expr).get_next(datetime).replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    except Exception:
        next_run = None
    with _connect(data_dir) as conn:
        conn.execute(
            """INSERT INTO schedules
               (schedule_id, cron_expr, template_id, workflow_id, params_json,
                enabled, next_run_at, created_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (sid, cron_expr, template_id, workflow_id,
             json.dumps(params or {}), next_run, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM schedules WHERE schedule_id=?", (sid,)).fetchone()
    return dict(row)


def list_schedules(data_dir: Path) -> list[dict[str, Any]]:
    with _connect(data_dir) as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_schedule(data_dir: Path, schedule_id: str) -> dict[str, Any] | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)
        ).fetchone()
    return dict(row) if row else None


def update_schedule(data_dir: Path, schedule_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"enabled", "cron_expr", "params_json"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_schedule(data_dir, schedule_id)
    vals.append(schedule_id)
    with _connect(data_dir) as conn:
        conn.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE schedule_id=?", vals)
        conn.commit()
    return get_schedule(data_dir, schedule_id)


def delete_schedule(data_dir: Path, schedule_id: str) -> bool:
    with _connect(data_dir) as conn:
        result = conn.execute("DELETE FROM schedules WHERE schedule_id=?", (schedule_id,))
        conn.commit()
    return result.rowcount > 0


def get_due_schedules(data_dir: Path) -> list[dict[str, Any]]:
    now = _now_iso()
    with _connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at<=?)",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def tick_schedule(data_dir: Path, schedule_id: str, cron_expr: str) -> None:
    """Update last_run_at and compute next_run_at after firing a schedule."""
    now = _now_iso()
    try:
        from croniter import croniter
        next_run = croniter(cron_expr).get_next(datetime).replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    except Exception:
        next_run = None
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE schedules SET last_run_at=?, next_run_at=? WHERE schedule_id=?",
            (now, next_run, schedule_id),
        )
        conn.commit()


# ── Backward-compat shim: load_store ─────────────────────────────────────────

def load_store(data_dir: Path) -> None:
    """Called by routes_orchestration on startup; ensures DB is ready."""
    init_db(data_dir)
