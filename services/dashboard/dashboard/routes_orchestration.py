"""Stable orchestration HTTP API (dashboard). Agents should prefer these verbs over raw gateway tool names."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dashboard.orchestration_db import (
    get_workflow_version,
    list_workflow_versions,
    load_store,
    promote_workflow_version,
    rollback_workflow,
    save_workflow_version,
)
from dashboard.orchestration_readiness import compute_readiness
from dashboard.secret_env import read_secret
from dashboard.text_sanitizers import sanitize_workflow_id
from dashboard.workflow_boundary import assert_api_workflow
from dashboard.workflow_templates import compile_template, list_template_ids, load_template

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
WORKFLOWS_DIR = Path(os.environ.get("COMFYUI_WORKFLOWS_DIR", "/comfyui-workflows")).resolve()
OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
OPS_CONTROLLER_TOKEN = read_secret("OPS_CONTROLLER_TOKEN")
# The ComfyUI GPU admission gate; empty when comfyui is not enabled.
COMFYUI_URL = os.environ.get("COMFYUI_URL", "").rstrip("/")


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
    # X-Actor names the caller in ops-controller's audit log.
    h: dict[str, str] = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}", "X-Actor": "orchestration"}
    if request and request.headers.get("X-Request-ID"):
        h["X-Request-ID"] = request.headers["X-Request-ID"]
    return h


DATA_DIR.mkdir(parents=True, exist_ok=True)
load_store(DATA_DIR)


# ── Readiness ──────────────────────────────────────────────────────────────────

@router.get("/readiness")
async def readiness():
    """Returns 200 when upstream services (model-gateway, MCP, ComfyUI) are healthy, 503 otherwise."""
    r = await asyncio.to_thread(compute_readiness)
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


# ── Outputs (replaces raw filesystem mount) ───────────────────────────────────

COMFYUI_OUTPUT_DIR = Path(os.environ.get("COMFYUI_OUTPUT_DIR", "/comfyui-output")).resolve()


@router.get("/outputs")
async def list_outputs():
    """List generated ComfyUI output files (replaces direct filesystem mount access)."""
    if not COMFYUI_OUTPUT_DIR.is_dir():
        return {"outputs": [], "output_dir": str(COMFYUI_OUTPUT_DIR)}
    files = []
    try:
        entries = []
        for p in COMFYUI_OUTPUT_DIR.iterdir():
            if p.is_file():
                st = p.stat()
                entries.append((p, st))
        entries.sort(key=lambda x: x[1].st_mtime, reverse=True)
        for p, st in entries:
            files.append({
                "filename": p.name,
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
                "suffix": p.suffix,
            })
    except OSError as e:
        logger.warning("Could not read output dir: %s", e)
    return {"outputs": files[:200], "output_dir": str(COMFYUI_OUTPUT_DIR)}


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


@router.get("/comfyui/status")
async def comfyui_status(request: Request):
    """Canonical ComfyUI health verb for agents.

    Deliberately ComfyUI-INDEPENDENT: it queries ops-controller (which stays up
    when ComfyUI is down), not ComfyUI directly — so an agent can reliably check
    state before/after a restart instead of guessing raw `/api/comfyui/*` paths.
    Returns container state + queue reachability + a rolled-up `up`.
    """
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="OPS_CONTROLLER_TOKEN not configured")
    container_state = "unknown"
    queue: dict[str, Any] = {"reachable": False}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sr = await client.get(f"{OPS_CONTROLLER_URL}/services", headers=_ops_headers(request))
            if sr.status_code < 400:
                for svc in sr.json().get("services", []):
                    if svc.get("id") == "comfyui":
                        container_state = svc.get("state", "unknown")
                        break
            # Probe ComfyUI's queue (through its gate) instead of relying on guardian_status,
            # which is only updated when COMFYUI_SERIALIZE_LLAMACPP is enabled.
            if COMFYUI_URL:
                try:
                    qr = await client.get(f"{COMFYUI_URL}/api/queue", timeout=5.0)
                    if qr.status_code < 400:
                        qdata = qr.json()
                        queue = {
                            "reachable": True,
                            "pending": len(qdata.get("queue_pending", [])),
                            "running": len(qdata.get("queue_running", [])),
                        }
                except Exception:
                    queue = {"reachable": False}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "service": "comfyui",
        "container_state": container_state,
        "queue": queue,
        "up": container_state == "running" and bool(queue.get("reachable")),
    }


# ── Registry passthrough (Hermes path) ────────────────────────────────────────

def _hermes_ops_headers(request: Request) -> dict[str, str]:
    """Like _ops_headers but adds X-Actor: hermes so ops-controller records the actor."""
    return {**_ops_headers(request), "X-Actor": "hermes"}


@router.get("/registry/models")
async def orch_registry_list_models(request: Request):
    """Hermes passthrough: list all managed models from ops-controller registry."""
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="OPS_CONTROLLER_TOKEN not configured")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{OPS_CONTROLLER_URL}/registry/models",
                headers=_hermes_ops_headers(request),
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/registry/gpus")
async def orch_registry_list_gpus(request: Request):
    """Hermes passthrough: live GPU VRAM/util + model assignments from ops-controller."""
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="OPS_CONTROLLER_TOKEN not configured")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{OPS_CONTROLLER_URL}/registry/gpus",
                headers=_hermes_ops_headers(request),
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


# ── GPU lease visibility (orchestration tab) ─────────────────────────────────────────────
# Pure presentation proxies: the scheduler records/serves the truth; the dashboard displays it.


@router.get("/gpu/history")
async def orchestration_gpu_history() -> dict[str, Any]:
    """Finished leases (newest first) from the scheduler's durable lease record."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # The scheduler lives in ops-controller, which requires the bearer token (#225).
            r = await client.get(f"{OPS_CONTROLLER_URL}/jobs/history", headers=_ops_headers(None))
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"scheduler unreachable: {e}") from e
