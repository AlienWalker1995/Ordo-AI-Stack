"""Execution receipts and job state (file-backed; dashboard is source of truth for orchestration)."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_STATE_LOCK = threading.Lock()


class JobState(StrEnum):
    queued = "queued"
    validated = "validated"
    running = "running"
    artifact_ready = "artifact_ready"
    publish_enqueued = "publish_enqueued"
    published = "published"
    failed = "failed"
    retried = "retried"


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
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


_JOBS: dict[str, OrchestrationJob] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _store_path(base: Path) -> Path:
    d = base / "orchestration"
    d.mkdir(parents=True, exist_ok=True)
    return d / "orchestration_jobs.json"


def _persist_unlocked(path: Path) -> None:
    payload = {k: {**v.to_dict()} for k, v in _JOBS.items()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_store(data_dir: Path) -> None:
    global _JOBS
    path = _store_path(data_dir)
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    with _STATE_LOCK:
        for jid, row in raw.items():
            st = row.get("state", "queued")
            try:
                state = JobState(st)
            except ValueError:
                state = JobState.queued
            _JOBS[jid] = OrchestrationJob(
                job_id=jid,
                state=state,
                created_at=row.get("created_at", _now_iso()),
                updated_at=row.get("updated_at", _now_iso()),
                template_id=row.get("template_id"),
                workflow_id=row.get("workflow_id"),
                prompt_id=row.get("prompt_id"),
                error=row.get("error"),
                outputs=row.get("outputs"),
                publish_webhook=row.get("publish_webhook"),
                publish_status=row.get("publish_status"),
                extra=row.get("extra") or {},
            )


def create_job(
    *,
    data_dir: Path,
    template_id: str | None = None,
    workflow_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> OrchestrationJob:
    jid = str(uuid.uuid4())
    t = _now_iso()
    job = OrchestrationJob(
        job_id=jid,
        state=JobState.queued,
        created_at=t,
        updated_at=t,
        template_id=template_id,
        workflow_id=workflow_id,
        extra=extra or {},
    )
    path = _store_path(data_dir)
    with _STATE_LOCK:
        _JOBS[jid] = job
        _persist_unlocked(path)
    return job


def update_job(data_dir: Path, job_id: str, **fields: Any) -> OrchestrationJob | None:
    path = _store_path(data_dir)
    with _STATE_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        for k, v in fields.items():
            if hasattr(job, k):
                setattr(job, k, v)
        job.updated_at = _now_iso()
        _persist_unlocked(path)
        return job


def get_job(job_id: str) -> OrchestrationJob | None:
    with _STATE_LOCK:
        j = _JOBS.get(job_id)
        return j
