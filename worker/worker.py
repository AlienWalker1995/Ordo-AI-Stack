#!/usr/bin/env python3
"""Durable job worker: polls SQLite queue, executes ComfyUI jobs, delivers publish outbox, fires schedules.

Runs as a separate container. Shares /data/dashboard volume with dashboard (SQLite WAL mode).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ── Bootstrap path so we can import dashboard modules ─────────────────────────
sys.path.insert(0, "/app")

from dashboard.orchestration_db import (
    JobState,
    OrchestrationJob,
    checkpoint_wal,
    claim_next_job,
    create_job,
    get_due_schedules,
    get_job,
    get_pending_outbox,
    load_store,
    mark_outbox_delivered,
    record_outbox_attempt,
    recover_stale_running_jobs,
    tick_schedule,
    update_job,
    vacuum_db,
)
from dashboard.param_placeholders import apply_param_placeholders
from dashboard.text_sanitizers import sanitize_workflow_id
from dashboard.workflow_boundary import assert_api_workflow
from dashboard.workflow_templates import compile_template, load_template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("worker")

DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_PATH", "/data/dashboard")).resolve()
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
WORKFLOWS_DIR = Path(os.environ.get("COMFYUI_WORKFLOWS_DIR", "/comfyui-workflows")).resolve()
WORKER_POLL_SEC = float(os.environ.get("WORKER_POLL_INTERVAL_SEC", "0.5"))
WORKER_CONCURRENCY = max(1, int(os.environ.get("WORKER_CONCURRENCY", "1")))
SCHEDULE_CHECK_SEC = float(os.environ.get("WORKER_SCHEDULE_CHECK_SEC", "30"))
WAL_CHECKPOINT_SEC = float(os.environ.get("WORKER_WAL_CHECKPOINT_SEC", "300"))
VACUUM_SEC = float(os.environ.get("WORKER_VACUUM_SEC", "86400"))
MAX_RETRIES = int(os.environ.get("WORKER_MAX_JOB_RETRIES", "2"))
PUBLISH_MAX_ATTEMPTS = int(os.environ.get("WORKER_PUBLISH_MAX_ATTEMPTS", "5"))
HEARTBEAT_PATH = Path("/tmp/worker.heartbeat")


# ── ComfyUI HTTP (inline; no async needed in worker) ─────────────────────────

_comfyui_client = httpx.Client(base_url=COMFYUI_URL, timeout=30)
_outbox_client = httpx.Client(timeout=30)


def _comfyui_post_prompt(workflow: dict[str, Any], client_id: str) -> str:
    body = {"prompt": workflow, "client_id": client_id}
    r = _comfyui_client.post("/prompt", json=body)
    r.raise_for_status()
    data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise RuntimeError(f"No prompt_id in response: {data}")
    return str(pid)


def _comfyui_wait_outputs(prompt_id: str, job_id: str, timeout: int = 600) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check for cancellation between polls
        fresh = get_job(DATA_DIR, job_id)
        if fresh and fresh.state == JobState.cancelling:
            raise RuntimeError(f"Job {job_id} cancelled during execution")
        try:
            r = _comfyui_client.get(f"/history/{prompt_id}", timeout=15)
            r.raise_for_status()
            history = r.json()
            entry = history.get(prompt_id, {})
            if not isinstance(entry, dict):
                logger.warning("Unexpected history format for %s: %s", prompt_id, type(entry).__name__)
                continue
            if entry.get("outputs"):
                return entry
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            logger.debug("ComfyUI history poll for %s: %s", prompt_id, exc)
        time.sleep(3)
    raise TimeoutError(f"ComfyUI did not finish prompt {prompt_id} within {timeout}s")


# ── Job execution ─────────────────────────────────────────────────────────────

def _resolve_workflow_path(workflow_id: str) -> Path | None:
    root = WORKFLOWS_DIR.resolve()
    normalized = sanitize_workflow_id(workflow_id)
    if not normalized:
        return None
    raw = normalized.replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return None
    if "/" in raw:
        rel = raw[:-5] if raw.lower().endswith(".json") else raw
        p = (root / rel).with_suffix(".json").resolve()
    else:
        safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
        p = (root / f"{safe}.json").resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


def execute_job(job: OrchestrationJob) -> None:
    import uuid as _uuid

    jid = job.job_id
    logger.info("Executing job %s (template=%s workflow=%s)", jid, job.template_id, job.workflow_id)

    # Check for cancellation before starting
    fresh = get_job(DATA_DIR, jid)
    if fresh and fresh.state == JobState.cancelling:
        update_job(DATA_DIR, jid, state=JobState.cancelled)
        logger.info("Job %s cancelled before execution", jid)
        return

    try:
        # Compile workflow (state is already validated from claim_next_job)
        if job.compiled_workflow:
            # Pre-compiled (e.g. retry or scheduled)
            wf = json.loads(job.compiled_workflow) if isinstance(job.compiled_workflow, str) else job.compiled_workflow
        elif job.template_id:
            tpl = load_template(job.template_id)
            params = json.loads(job.params_json) if job.params_json else {}
            wf = compile_template(tpl, params, workflows_dir=WORKFLOWS_DIR)
        elif job.workflow_id:
            path = _resolve_workflow_path(job.workflow_id)
            if not path:
                raise ValueError(f"Invalid workflow_id: {job.workflow_id!r}")
            wf = json.loads(path.read_text(encoding="utf-8"))
            assert_api_workflow(wf)
            params = json.loads(job.params_json) if job.params_json else {}
            wf = apply_param_placeholders(wf, params)
        else:
            raise ValueError("Job has neither template_id, workflow_id, nor compiled_workflow")

        # Store compiled workflow for retry durability
        update_job(DATA_DIR, jid, state=JobState.running,
                   compiled_workflow=json.dumps(wf) if isinstance(wf, dict) else wf)

        client_id = str(_uuid.uuid4())
        pid = _comfyui_post_prompt(wf, client_id)
        update_job(DATA_DIR, jid, prompt_id=pid)

        entry = _comfyui_wait_outputs(pid, jid)
        update_job(DATA_DIR, jid, state=JobState.artifact_ready, outputs=entry.get("outputs", {}))
        logger.info("Job %s completed successfully (prompt_id=%s)", jid, pid)

    except Exception as exc:
        logger.exception("Job %s failed", jid)
        retry_count = (job.retry_count or 0) + 1
        if retry_count <= MAX_RETRIES:
            try:
                update_job(DATA_DIR, jid, state=JobState.failed,
                           error=f"attempt {retry_count - 1} failed: {exc}"[:4096])
                params = json.loads(job.params_json) if job.params_json else {}
                compiled = (json.loads(job.compiled_workflow)
                            if isinstance(job.compiled_workflow, str) and job.compiled_workflow
                            else job.compiled_workflow if isinstance(job.compiled_workflow, dict)
                            else None)
                new_job = create_job(
                    DATA_DIR,
                    template_id=job.template_id,
                    workflow_id=job.workflow_id,
                    params=params,
                    compiled_workflow=compiled,
                    extra={"retried_from": jid, "retry_count": retry_count},
                )
                update_job(DATA_DIR, new_job.job_id, retry_count=retry_count)
                logger.info("Job %s failed; requeued (attempt %d/%d)", jid, retry_count, MAX_RETRIES + 1)
            except Exception as retry_exc:
                logger.error("Job %s retry failed: %s", jid, retry_exc)
                update_job(DATA_DIR, jid, state=JobState.failed,
                           error=f"retry failed: {retry_exc}"[:4096])
        else:
            update_job(DATA_DIR, jid, state=JobState.failed, error=str(exc)[:4096])
            logger.error("Job %s permanently failed after %d attempts", jid, retry_count)


# ── Outbox delivery ───────────────────────────────────────────────────────────

def process_outbox() -> None:
    entries = get_pending_outbox(DATA_DIR, max_attempts=PUBLISH_MAX_ATTEMPTS)
    for entry in entries:
        key = entry.get("idempotency_key")
        row_id = entry["id"]
        try:
            r = _outbox_client.post(
                entry["webhook_url"],
                json=json.loads(entry["payload_json"]),
                headers={"X-Idempotency-Key": key or ""},
            )
            r.raise_for_status()
            mark_outbox_delivered(DATA_DIR, key)
            # Transition job to published
            job = get_job(DATA_DIR, entry["job_id"])
            if job and job.state == JobState.publish_enqueued:
                update_job(DATA_DIR, entry["job_id"], state=JobState.published,
                           publish_status="published")
            logger.info("Outbox entry %d delivered for job %s", row_id, entry["job_id"])
        except Exception as exc:
            record_outbox_attempt(DATA_DIR, row_id, error=str(exc))
            logger.warning("Outbox entry %d delivery failed: %s", row_id, exc)


# ── Schedule firing ───────────────────────────────────────────────────────────

def fire_due_schedules() -> None:
    due = get_due_schedules(DATA_DIR)
    for sched in due:
        sid = sched["schedule_id"]
        try:
            params = json.loads(sched.get("params_json") or "{}")
            create_job(
                DATA_DIR,
                template_id=sched.get("template_id"),
                workflow_id=sched.get("workflow_id"),
                params=params,
                extra={"fired_by_schedule": sid},
            )
            tick_schedule(DATA_DIR, sid, sched["cron_expr"])
            logger.info("Fired schedule %s", sid)
        except Exception as exc:
            logger.error("Failed to fire schedule %s: %s", sid, exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

_shutdown_requested = False


def _handle_shutdown(signum: int, _frame: Any) -> None:
    global _shutdown_requested  # noqa: PLW0603
    logger.info("Received signal %s — draining in-flight jobs before exit", signal.Signals(signum).name)
    _shutdown_requested = True


def main() -> None:
    global _shutdown_requested  # noqa: PLW0603
    logger.info(
        "Worker starting. DATA_DIR=%s COMFYUI_URL=%s CONCURRENCY=%s",
        DATA_DIR,
        COMFYUI_URL,
        WORKER_CONCURRENCY,
    )
    load_store(DATA_DIR)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    recovered = recover_stale_running_jobs(DATA_DIR)
    if recovered:
        logger.warning("Recovered %d stale running/validated jobs → requeued", recovered)

    last_schedule_check = 0.0
    last_wal_checkpoint = 0.0
    last_vacuum = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY) as pool:
        inflight: dict[concurrent.futures.Future[None], str] = {}

        while not _shutdown_requested:
            done = [future for future in inflight if future.done()]
            for future in done:
                jid = inflight.pop(future, "")
                try:
                    future.result()
                except Exception:
                    logger.exception("Worker thread crashed while processing job %s", jid)

            while len(inflight) < WORKER_CONCURRENCY:
                job = claim_next_job(DATA_DIR)
                if not job:
                    break
                future = pool.submit(execute_job, job)
                inflight[future] = job.job_id

            try:
                process_outbox()
            except Exception as exc:
                logger.error("Outbox processing error: %s", exc)

            if time.time() - last_schedule_check >= SCHEDULE_CHECK_SEC:
                try:
                    fire_due_schedules()
                except Exception as exc:
                    logger.error("Schedule check error: %s", exc)
                last_schedule_check = time.time()

            if time.time() - last_wal_checkpoint >= WAL_CHECKPOINT_SEC:
                try:
                    checkpoint_wal(DATA_DIR)
                except Exception as exc:
                    logger.error("WAL checkpoint error: %s", exc)
                last_wal_checkpoint = time.time()

            if time.time() - last_vacuum >= VACUUM_SEC and not inflight:
                pool.submit(vacuum_db, DATA_DIR)
                last_vacuum = time.time()

            HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")
            time.sleep(WORKER_POLL_SEC)

        # Drain in-flight jobs before exiting
        if inflight:
            logger.info("Waiting for %d in-flight job(s) to finish...", len(inflight))
            for future in concurrent.futures.as_completed(inflight, timeout=120):
                jid = inflight.get(future, "")
                try:
                    future.result()
                except Exception:
                    logger.exception("Job %s failed during shutdown drain", jid)
            logger.info("All in-flight jobs drained.")

        # Final WAL checkpoint — ensure all writes are flushed to the main DB file
        try:
            checkpoint_wal(DATA_DIR)
        except Exception as exc:
            logger.error("Final WAL checkpoint failed: %s", exc)

    logger.info("Worker shut down gracefully.")


if __name__ == "__main__":
    main()
