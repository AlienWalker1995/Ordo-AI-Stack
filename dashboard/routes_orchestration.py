"""Stable orchestration HTTP API (dashboard). Agents should prefer these verbs over raw gateway tool names."""

from __future__ import annotations

import difflib
import ipaddress
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dashboard.orchestration_db import (
    JobState,
    cancel_job,
    create_job,
    create_outbox_entry,
    create_schedule,
    delete_schedule,
    get_job,
    get_workflow_version,
    list_jobs,
    list_schedules,
    list_workflow_versions,
    load_store,
    mark_outbox_delivered,
    promote_workflow_version,
    rollback_workflow,
    save_workflow_version,
    update_job,
    update_schedule,
)
from dashboard.orchestration_readiness import compute_readiness
from dashboard.text_sanitizers import sanitize_workflow_id
from dashboard.workflow_boundary import assert_api_workflow
from dashboard.workflow_templates import compile_template, list_template_ids, load_template

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
WORKFLOWS_DIR = Path(os.environ.get("COMFYUI_WORKFLOWS_DIR", "/comfyui-workflows")).resolve()
N8N_PUBLISH_WEBHOOK_URL = os.environ.get("N8N_PUBLISH_WEBHOOK_URL", "").strip()
OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()


def _resolve_workflow_under_root(workflow_id: str, root: Path) -> Path | None:
    root = root.resolve()
    raw = workflow_id.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return None
    if "/" in raw:
        rel = raw[:-5] if raw.lower().endswith(".json") else raw
        p = (root / rel).with_suffix(".json").resolve()
    else:
        safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
        if not safe:
            return None
        p = (root / f"{safe}.json").resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


def _safe_workflow_path(workflow_id: str) -> Path | None:
    return _resolve_workflow_under_root(workflow_id, WORKFLOWS_DIR)


def _ops_headers(request: Request | None) -> dict[str, str]:
    if not OPS_CONTROLLER_TOKEN:
        return {}
    h: dict[str, str] = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}"}
    if request and request.headers.get("X-Request-ID"):
        h["X-Request-ID"] = request.headers["X-Request-ID"]
    return h


DATA_DIR.mkdir(parents=True, exist_ok=True)
load_store(DATA_DIR)


# ── Readiness ──────────────────────────────────────────────────────────────────

@router.get("/readiness")
async def readiness():
    """Returns 200 when all upstream services (ComfyUI, worker) are healthy, 503 otherwise."""
    r = compute_readiness()
    if not r.get("ok"):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=r)
    return r


# ── Workflows ─────────────────────────────────────────────────────────────────

@router.get("/workflows")
async def list_workflows_endpoint():
    """List all available workflows (templates + JSON files in the workflows directory)."""
    templates = [{"id": tid, "kind": "template"} for tid in list_template_ids()]
    files: list[dict[str, str]] = []
    try:
        if WORKFLOWS_DIR.is_dir():
            for p in sorted(WORKFLOWS_DIR.rglob("*.json")):
                if p.name.endswith(".meta.json"):
                    continue
                rel = p.relative_to(WORKFLOWS_DIR)
                wid = str(rel.with_suffix("")).replace("\\", "/")
                files.append({"id": wid, "kind": "file"})
    except OSError as e:
        logger.warning("Could not read workflows dir: %s", e)
    return {"templates": templates, "workflow_files": files, "workflows_dir": str(WORKFLOWS_DIR)}


class ValidateBody(BaseModel):
    workflow: dict[str, Any] | None = None
    workflow_id: str | None = None


@router.post("/validate")
async def validate_workflow(body: ValidateBody):
    """Validate a workflow JSON body or a stored workflow_id against the ComfyUI API schema."""
    wf: dict[str, Any]
    if body.workflow is not None:
        wf = body.workflow
    elif body.workflow_id:
        workflow_id = sanitize_workflow_id(body.workflow_id)
        path = _safe_workflow_path(workflow_id or "")
        if not path:
            raise HTTPException(status_code=400, detail="Invalid workflow_id. Use alphanumeric characters, hyphens, or underscores (no leading slashes or '..' segments).")
        wf = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise HTTPException(status_code=400, detail="Provide workflow or workflow_id")
    try:
        assert_api_workflow(wf)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "format": "api"}


class FromTemplateBody(BaseModel):
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/workflows/from-template")
async def create_from_template(body: FromTemplateBody):
    try:
        tpl = load_template(body.template_id)
        compiled = compile_template(tpl, body.params, workflows_dir=WORKFLOWS_DIR)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "workflow": compiled, "template_id": body.template_id}


# ── Workflow lifecycle ────────────────────────────────────────────────────────

class SaveWorkflowBody(BaseModel):
    workflow_id: str
    workflow: dict[str, Any]
    params_schema: dict[str, Any] | None = None


@router.post("/workflows/save")
async def save_workflow(body: SaveWorkflowBody):
    """Validate and save a compiled workflow as a new version."""
    try:
        assert_api_workflow(body.workflow)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    workflow_id = sanitize_workflow_id(body.workflow_id)
    if not workflow_id:
        raise HTTPException(status_code=400, detail="Invalid workflow_id. Use alphanumeric characters, hyphens, or underscores (no leading slashes or '..' segments).")
    version = save_workflow_version(DATA_DIR, workflow_id, body.workflow, body.params_schema)
    return {"ok": True, "workflow_id": workflow_id, "version": version}


@router.get("/workflows/{workflow_id}/versions")
async def workflow_versions(workflow_id: str):
    return {"workflow_id": workflow_id, "versions": list_workflow_versions(DATA_DIR, workflow_id)}


@router.get("/workflows/{workflow_id}/versions/{version}")
async def workflow_version(workflow_id: str, version: int):
    v = get_workflow_version(DATA_DIR, workflow_id, version)
    if not v:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@router.post("/workflows/{workflow_id}/diff")
async def diff_workflow_versions(workflow_id: str, v1: int = Query(...), v2: int = Query(...)):
    a = get_workflow_version(DATA_DIR, workflow_id, v1)
    b = get_workflow_version(DATA_DIR, workflow_id, v2)
    if not a or not b:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    a_lines = json.dumps(a.get("compiled_json") or {}, indent=2).splitlines(keepends=True)
    b_lines = json.dumps(b.get("compiled_json") or {}, indent=2).splitlines(keepends=True)
    diff = list(difflib.unified_diff(a_lines, b_lines, fromfile=f"v{v1}", tofile=f"v{v2}"))
    return {"workflow_id": workflow_id, "v1": v1, "v2": v2, "diff": "".join(diff)}


@router.post("/workflows/{workflow_id}/promote")
async def promote_workflow(workflow_id: str, version: int = Query(...)):
    ok = promote_workflow_version(DATA_DIR, workflow_id, version)
    if not ok:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"ok": True, "workflow_id": workflow_id, "promoted_version": version}


@router.post("/workflows/{workflow_id}/rollback")
async def rollback_workflow_endpoint(workflow_id: str, to_version: int = Query(...)):
    new_v = rollback_workflow(DATA_DIR, workflow_id, to_version)
    if new_v is None:
        raise HTTPException(status_code=404, detail="Source version not found")
    return {"ok": True, "workflow_id": workflow_id, "new_version": new_v, "rolled_back_to": to_version}


# ── Job execution ─────────────────────────────────────────────────────────────

class RunBody(BaseModel):
    template_id: str | None = None
    workflow_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    await_completion: bool = False  # kept for API compatibility; worker handles execution


@router.post("/run")
async def run_workflow(body: RunBody):
    """Queue a job for the worker. Returns job_id immediately."""
    r = compute_readiness()
    if not r.get("ok"):
        raise HTTPException(status_code=503, detail={"readiness": r})
    workflow_id = sanitize_workflow_id(body.workflow_id)
    if not body.template_id and not workflow_id:
        raise HTTPException(status_code=400, detail="template_id or workflow_id required")
    job = create_job(
        DATA_DIR,
        template_id=body.template_id,
        workflow_id=workflow_id,
        params=body.params,
    )
    return {"job_id": job.job_id, "state": JobState.queued.value}


@router.get("/jobs")
async def list_jobs_endpoint(state: str | None = None, limit: int = 100):
    """List orchestration jobs, optionally filtered by state (queued, running, failed, etc.)."""
    limit = max(1, min(limit, 1000))
    if state is not None:
        try:
            JobState(state)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid state. Must be one of: {[s.value for s in JobState]}")
    jobs = list_jobs(DATA_DIR, state=state, limit=limit)
    return {"jobs": [j.to_dict() for j in jobs], "count": len(jobs)}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    j = get_job(DATA_DIR, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return j.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str):
    j = cancel_job(DATA_DIR, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"ok": True, "job_id": job_id, "state": j.state.value}


# ── Publish pipeline ──────────────────────────────────────────────────────────

class PublishEnqueueBody(BaseModel):
    job_id: str
    webhook_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/publish/enqueue")
async def publish_enqueue(body: PublishEnqueueBody):
    """Write to durable outbox (worker delivers with retries). No live HTTP call here."""
    url = (body.webhook_url or N8N_PUBLISH_WEBHOOK_URL).strip()
    if not url:
        raise HTTPException(
            status_code=503,
            detail="Set N8N_PUBLISH_WEBHOOK_URL or pass webhook_url.",
        )
    # SSRF protection: reject internal/private IPs
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="webhook_url must use http or https")
        hostname = parsed.hostname or ""
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _, _, _, _, addr in resolved:
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                # Allow Docker internal network (common for n8n webhooks)
                if not (ip.is_private and hostname.endswith((".internal", ".local")) or "." not in hostname):
                    pass  # Docker service names are OK
    except (ValueError, socket.gaierror):
        pass  # Allow unresolvable hostnames (Docker DNS resolves at delivery time)
    j = get_job(DATA_DIR, body.job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    envelope = {
        "job_id": body.job_id,
        "state": j.state.value,
        "outputs": j.outputs,
        "payload": body.payload,
    }
    idem_key = create_outbox_entry(DATA_DIR, body.job_id, url, envelope)
    update_job(DATA_DIR, body.job_id, state=JobState.publish_enqueued,
               publish_webhook=url, publish_status="enqueued")
    return {"ok": True, "job_id": body.job_id, "state": JobState.publish_enqueued.value,
            "idempotency_key": idem_key}


class PublishCallbackBody(BaseModel):
    job_id: str
    status: str  # "delivered" | "failed"
    idempotency_key: str | None = None
    platform: str | None = None
    post_url: str | None = None
    error: str | None = None


@router.post("/publish/callback")
async def publish_callback(body: PublishCallbackBody):
    """Called by n8n after delivering to the social platform."""
    j = get_job(DATA_DIR, body.job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    if body.status == "delivered":
        update_job(DATA_DIR, body.job_id, state=JobState.published,
                   publish_status="published")
        if body.idempotency_key:
            mark_outbox_delivered(DATA_DIR, body.idempotency_key)
    else:
        update_job(DATA_DIR, body.job_id, publish_status=f"failed: {body.error or 'unknown'}")
    return {"ok": True, "job_id": body.job_id}


@router.get("/publish/status")
async def publish_status(job_id: str):
    j = get_job(DATA_DIR, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {
        "job_id": job_id,
        "state": j.state.value,
        "publish_webhook": j.publish_webhook,
        "publish_status": j.publish_status,
    }


# ── Outputs (replaces raw filesystem mount) ───────────────────────────────────

COMFYUI_OUTPUT_DIR = Path(os.environ.get("COMFYUI_OUTPUT_DIR", "/comfyui-output")).resolve()


@router.get("/outputs")
async def list_outputs():
    """List generated ComfyUI output files (replaces direct filesystem mount access)."""
    if not COMFYUI_OUTPUT_DIR.is_dir():
        return {"outputs": [], "output_dir": str(COMFYUI_OUTPUT_DIR)}
    files = []
    try:
        for p in sorted(COMFYUI_OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                files.append({
                    "filename": p.name,
                    "size_bytes": p.stat().st_size,
                    "modified_at": p.stat().st_mtime,
                    "suffix": p.suffix,
                })
    except OSError as e:
        logger.warning("Could not read output dir: %s", e)
    return {"outputs": files[:200], "output_dir": str(COMFYUI_OUTPUT_DIR)}


# ── Schedules ─────────────────────────────────────────────────────────────────

class CreateScheduleBody(BaseModel):
    cron_expr: str
    template_id: str | None = None
    workflow_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/schedules")
async def create_schedule_endpoint(body: CreateScheduleBody):
    """Create a cron-scheduled workflow run. Requires template_id or workflow_id and a cron expression."""
    workflow_id = sanitize_workflow_id(body.workflow_id)
    if not body.template_id and not workflow_id:
        raise HTTPException(status_code=400, detail="template_id or workflow_id required")
    try:
        s = create_schedule(DATA_DIR, body.cron_expr, body.template_id, workflow_id, body.params)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return s


@router.get("/schedules")
async def list_schedules_endpoint():
    return {"schedules": list_schedules(DATA_DIR)}


class UpdateScheduleBody(BaseModel):
    enabled: bool | None = None
    cron_expr: str | None = None


@router.patch("/schedules/{schedule_id}")
async def update_schedule_endpoint(schedule_id: str, body: UpdateScheduleBody):
    fields: dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.cron_expr is not None:
        try:
            from croniter import croniter
            croniter(body.cron_expr)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
        fields["cron_expr"] = body.cron_expr
    s = update_schedule(DATA_DIR, schedule_id, **fields)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return s


@router.delete("/schedules/{schedule_id}")
async def delete_schedule_endpoint(schedule_id: str):
    ok = delete_schedule(DATA_DIR, schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"ok": True}


# ── ComfyUI ops ───────────────────────────────────────────────────────────────

class RestartBody(BaseModel):
    confirm: bool = False


@router.post("/comfyui/restart")
async def restart_comfyui(request: Request, body: RestartBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="OPS_CONTROLLER_TOKEN not configured")
    url = f"{OPS_CONTROLLER_URL}/services/comfyui/restart"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=_ops_headers(request), json={"confirm": True})
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
