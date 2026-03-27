"""Stable orchestration HTTP API (dashboard). Agents should prefer these verbs over raw gateway tool names."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from dashboard.comfyui_api_client import queue_prompt, wait_for_outputs
from dashboard.orchestration_jobs import JobState, create_job, get_job, load_store, update_job
from dashboard.orchestration_readiness import COMFYUI_URL, compute_readiness
from dashboard.param_placeholders import apply_param_placeholders
from dashboard.workflow_boundary import assert_api_workflow
from dashboard.workflow_templates import compile_template, list_template_ids, load_template

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_PATH", "/data/dashboard")).resolve()
WORKFLOWS_DIR = Path(os.environ.get("COMFYUI_WORKFLOWS_DIR", "/comfyui-workflows")).resolve()
N8N_PUBLISH_WEBHOOK_URL = os.environ.get("N8N_PUBLISH_WEBHOOK_URL", "").strip()

OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()


def _ops_headers(request: Request | None) -> dict[str, str]:
    if not OPS_CONTROLLER_TOKEN:
        return {}
    h: dict[str, str] = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}"}
    if request and request.headers.get("X-Request-ID"):
        h["X-Request-ID"] = request.headers["X-Request-ID"]
    return h


load_store(DATA_DIR)


@router.get("/readiness")
async def readiness():
    """Capability gate: model-gateway /ready, MCP gateway reachable, optional ComfyUI + workflow dir."""
    r = compute_readiness()
    if not r.get("ok"):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=r)
    return r


@router.get("/workflows")
async def list_workflows():
    """List builtin template ids and workflow *.json stems under the workflows directory."""
    templates = [{"id": tid, "kind": "template"} for tid in list_template_ids()]
    files: list[dict[str, str]] = []
    if WORKFLOWS_DIR.is_dir():
        for p in sorted(WORKFLOWS_DIR.rglob("*.json")):
            if p.name.endswith(".meta.json"):
                continue
            rel = p.relative_to(WORKFLOWS_DIR)
            wid = str(rel.with_suffix("")).replace("\\", "/")
            files.append({"id": wid, "kind": "file"})
    return {"templates": templates, "workflow_files": files, "workflows_dir": str(WORKFLOWS_DIR)}


class ValidateBody(BaseModel):
    workflow: dict[str, Any] | None = None
    workflow_id: str | None = None


@router.post("/validate")
async def validate_workflow(body: ValidateBody):
    """Reject UI-format JSON; optionally load workflow_id from disk."""
    wf: dict[str, Any]
    if body.workflow is not None:
        wf = body.workflow
    elif body.workflow_id:
        path = _safe_workflow_path(body.workflow_id)
        if not path:
            raise HTTPException(status_code=400, detail="Invalid workflow_id")
        wf = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise HTTPException(status_code=400, detail="Provide workflow or workflow_id")
    try:
        assert_api_workflow(wf)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "format": "api"}


def _safe_workflow_path(workflow_id: str) -> Path | None:
    raw = workflow_id.strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return None
    root = WORKFLOWS_DIR.resolve()
    if "/" in raw:
        rel = raw[:-5] if raw.lower().endswith(".json") else raw
        p = (WORKFLOWS_DIR / rel).with_suffix(".json").resolve()
    else:
        safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
        if not safe:
            return None
        p = (WORKFLOWS_DIR / f"{safe}.json").resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


class FromTemplateBody(BaseModel):
    template_id: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/workflows/from-template")
async def create_from_template(body: FromTemplateBody):
    """Validate params against template schema and compile to API-format graph (no disk write)."""
    try:
        tpl = load_template(body.template_id)
        compiled = compile_template(tpl, body.params, workflows_dir=WORKFLOWS_DIR)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "workflow": compiled, "template_id": body.template_id}


class RunBody(BaseModel):
    template_id: str | None = None
    workflow_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    await_completion: bool = False


async def _execute_job(job_id: str, body: RunBody) -> None:
    try:
        update_job(DATA_DIR, job_id, state=JobState.validated)
        if body.template_id:
            tpl = load_template(body.template_id)
            wf = compile_template(tpl, body.params, workflows_dir=WORKFLOWS_DIR)
        elif body.workflow_id:
            path = _safe_workflow_path(body.workflow_id)
            if not path:
                raise ValueError("Invalid workflow_id")
            wf = json.loads(path.read_text(encoding="utf-8"))
            assert_api_workflow(wf)
            wf = apply_param_placeholders(wf, body.params)
        else:
            raise ValueError("template_id or workflow_id required")

        update_job(DATA_DIR, job_id, state=JobState.running)
        pid = await queue_prompt(COMFYUI_URL, wf)
        update_job(DATA_DIR, job_id, prompt_id=pid)
        out = await wait_for_outputs(pid, COMFYUI_URL)
        update_job(
            DATA_DIR,
            job_id,
            state=JobState.artifact_ready,
            outputs=out,
        )
    except Exception as e:
        logger.exception("Job %s failed", job_id)
        update_job(DATA_DIR, job_id, state=JobState.failed, error=str(e))


@router.post("/run")
async def run_workflow(body: RunBody):
    """Queue execution; returns job_id. Use GET /jobs/{id} to poll (receipts)."""
    r = compute_readiness()
    if not r.get("ok"):
        raise HTTPException(status_code=503, detail={"readiness": r})

    job = create_job(
        DATA_DIR,
        template_id=body.template_id,
        workflow_id=body.workflow_id,
    )
    asyncio.create_task(_execute_job(job.job_id, body))
    return {"job_id": job.job_id, "state": JobState.queued.value}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    """Execution receipt: state, prompt_id, outputs, errors."""
    j = get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return j.to_dict()


class PublishEnqueueBody(BaseModel):
    job_id: str
    webhook_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/publish/enqueue")
async def publish_enqueue(body: PublishEnqueueBody):
    """Enqueue social publish to n8n (executor of record). OpenClaw must not be the durable publisher."""
    url = (body.webhook_url or N8N_PUBLISH_WEBHOOK_URL).strip()
    if not url:
        raise HTTPException(
            status_code=503,
            detail="Set N8N_PUBLISH_WEBHOOK_URL or pass webhook_url (n8n webhook path).",
        )
    j = get_job(body.job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")

    envelope = {
        "job_id": body.job_id,
        "state": j.state.value,
        "outputs": j.outputs,
        "payload": body.payload,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=envelope)
        r.raise_for_status()
    except Exception as e:
        update_job(DATA_DIR, body.job_id, publish_status=f"failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e

    update_job(
        DATA_DIR,
        body.job_id,
        state=JobState.publish_enqueued,
        publish_webhook=url,
        publish_status="enqueued",
    )
    return {"ok": True, "job_id": body.job_id, "state": JobState.publish_enqueued.value}


@router.get("/publish/status")
async def publish_status(job_id: str):
    j = get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {
        "job_id": job_id,
        "state": j.state.value,
        "publish_webhook": j.publish_webhook,
        "publish_status": j.publish_status,
    }


class RestartBody(BaseModel):
    confirm: bool = False


@router.post("/comfyui/restart")
async def restart_comfyui(request: Request, body: RestartBody):
    """Privileged restart via ops-controller (not raw MCP)."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm: true")
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="OPS_CONTROLLER_TOKEN not configured")
    url = f"{OPS_CONTROLLER_URL}/services/comfyui/restart"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                url,
                headers=_ops_headers(request),
                json={"confirm": True},
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
