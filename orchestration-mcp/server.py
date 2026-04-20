#!/usr/bin/env python3
"""MCP adapter with stable tool names; delegates to dashboard /api/orchestration (HTTP control plane)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("ORCHESTRATION_DASHBOARD_URL", "http://dashboard:8080").rstrip("/")
TOKEN = os.environ.get("DASHBOARD_AUTH_TOKEN", "").strip()


def _clean_gemma_special_tokens(text: str) -> str:
    """Replace Gemma special tokens (<|"|>, etc.) with literal characters."""
    if "<|" not in text:
        return text
    text = text.replace('<|"|>', '"')
    text = text.replace("<|'|>", "'")
    text = text.replace("<|`|>", "`")
    text = text.replace("<|\\n|>", "\n")
    return re.sub(r"<\|(.)\|>", r"\1", text)


def _sanitize_workflow_id(workflow_id: str | None) -> str | None:
    if workflow_id is None:
        return None
    cleaned = _clean_gemma_special_tokens(str(workflow_id)).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"', "`"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as client:
        r = client.get(f"{BASE}{path}", headers=_headers(), params=params or {})
        r.raise_for_status()
        return r.json()


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=120.0) as client:
        r = client.post(f"{BASE}{path}", headers=_headers(), json=body)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except (ValueError, UnicodeDecodeError):
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


def _patch(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.patch(f"{BASE}{path}", headers=_headers(), json=body)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except (ValueError, UnicodeDecodeError):
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


def _delete(path: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.delete(f"{BASE}{path}", headers=_headers())
        if r.status_code >= 400:
            try:
                detail = r.json()
            except (ValueError, UnicodeDecodeError):
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


mcp = FastMCP("orchestration")


# ── Readiness ──────────────────────────────────────────────────────────────────

@mcp.tool()
def orchestration_readiness() -> dict:
    """Return capability readiness (model-gateway, MCP gateway, optional ComfyUI)."""
    with httpx.Client(timeout=15.0) as client:
        r = client.get(f"{BASE}/api/orchestration/readiness")
        return r.json()


# ── Workflow lifecycle ────────────────────────────────────────────────────────

@mcp.tool()
def list_templates() -> dict:
    """List available typed templates (generate_image, generate_video, etc.) that can be used with create_from_template and run_workflow. Use this dedicated tool instead of generic gateway call tools for this operation."""
    result = _get("/api/orchestration/workflows")
    return {"templates": result.get("templates", [])}


@mcp.tool()
def list_workflows() -> dict:
    """List typed templates and workflow API files. Use this dedicated tool instead of generic gateway call tools for this operation."""
    return _get("/api/orchestration/workflows")


@mcp.tool()
def validate_workflow(workflow_json: str | None = None, workflow_id: str | None = None) -> dict:
    """Validate API-format workflow JSON; rejects ComfyUI UI/editor exports. Only API-format workflows are supported; UI/Editor exports from the ComfyUI web interface are invalid and will result in failure. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    body: dict[str, Any] = {}
    if workflow_json:
        try:
            body["workflow"] = json.loads(workflow_json)
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON in workflow_json: {e}"}
    workflow_id = _sanitize_workflow_id(workflow_id)
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/validate", body)


@mcp.tool()
def create_from_template(template_id: str, params_json: str = "{}") -> dict:
    """Compile a typed template to API-format graph. Use this dedicated tool instead of generic gateway call tools for this operation."""
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in params_json: {e}"}
    return _post("/api/orchestration/workflows/from-template",
                 {"template_id": template_id, "params": params})


@mcp.tool()
def save_workflow(workflow_id: str, workflow_json: str, params_schema_json: str = "{}") -> dict:
    """Save a compiled API-format workflow as a new versioned snapshot. Use this dedicated tool instead of generic gateway call tools for this operation. Only API-format workflows are supported; UI/Editor exports from the ComfyUI web interface are invalid. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix. DISCOVERY REQUIRED: Perform a fresh discovery via list_workflows or list_templates to verify the current ID before execution; do not rely on IDs from memory."""
    try:
        workflow = json.loads(workflow_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in workflow_json: {e}"}
    try:
        params_schema = json.loads(params_schema_json) if params_schema_json else None
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in params_schema_json: {e}"}
    workflow_id = _sanitize_workflow_id(workflow_id)
    return _post("/api/orchestration/workflows/save",
                 {"workflow_id": workflow_id, "workflow": workflow, "params_schema": params_schema})


@mcp.tool()
def list_workflow_versions(workflow_id: str) -> dict:
    """List all saved versions of a workflow. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _get(f"/api/orchestration/workflows/{workflow_id}/versions")


@mcp.tool()
def diff_workflow_versions(workflow_id: str, v1: int, v2: int) -> dict:
    """Unified diff between two saved workflow versions. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/diff", {"v1": v1, "v2": v2})


@mcp.tool()
def promote_workflow(workflow_id: str, version: int) -> dict:
    """Mark a workflow version as the active promoted version. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/promote?version={version}", {})


@mcp.tool()
def rollback_workflow(workflow_id: str, to_version: int) -> dict:
    """Create a new version by copying an older version (rollback). CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/rollback?to_version={to_version}", {})


# ── Job execution ─────────────────────────────────────────────────────────────

@mcp.tool()
def run_workflow(
    template_id: str | None = None,
    workflow_id: str | None = None,
    params_json: str = "{}",
) -> dict:
    """Queue a workflow run via the worker. Returns job_id immediately. Use this dedicated tool instead of generic gateway call tools. Only API-format workflows are supported; UI/Editor exports from the ComfyUI web interface are invalid. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix. MANDATORY SEQUENCE: After triggering a run, you MUST call await_run with the returned job_id to monitor state. Do not attempt to fetch outputs or assume completion until await_run returns a terminal state (completed/failed). DISCOVERY REQUIRED: Perform a fresh discovery via list_workflows or list_templates to verify the current ID before execution."""
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in params_json: {e}"}
    body: dict[str, Any] = {"params": params}
    if template_id:
        body["template_id"] = template_id
    workflow_id = _sanitize_workflow_id(workflow_id)
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/run", body)


@mcp.tool()
def await_run(job_id: str) -> dict:
    """Get execution receipt and current state for a job. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix. DISCOVERY REQUIRED: Always verify the job_id is current and valid."""
    return _get(f"/api/orchestration/jobs/{job_id}")


@mcp.tool()
def list_jobs(state: str | None = None, limit: int = 20) -> dict:
    """List recent jobs, optionally filtered by state. Use this dedicated tool instead of generic gateway call tools for this operation."""
    params: dict[str, Any] = {"limit": limit}
    if state:
        params["state"] = state
    return _get("/api/orchestration/jobs", params=params)


@mcp.tool()
def cancel_run(job_id: str) -> dict:
    """Request cancellation of a queued or validated job. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/jobs/{job_id}/cancel", {})


# ── Publish pipeline ──────────────────────────────────────────────────────────

@mcp.tool()
def publish_enqueue(job_id: str, webhook_url: str | None = None, payload_json: str = "{}") -> dict:
    """Write to durable publish outbox (worker delivers with retries to n8n). CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in payload_json: {e}"}
    body: dict[str, Any] = {"job_id": job_id, "payload": payload}
    if webhook_url:
        body["webhook_url"] = webhook_url
    return _post("/api/orchestration/publish/enqueue", body)


@mcp.tool()
def publish_status(job_id: str) -> dict:
    """Publish pipeline status and delivery history for a job. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _get("/api/orchestration/publish/status", params={"job_id": job_id})


# ── Outputs ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_outputs() -> dict:
    """List generated ComfyUI output files via the API (no filesystem mount required). Use this dedicated tool instead of generic gateway call tools for this operation."""
    return _get("/api/orchestration/outputs")


# ── Schedules ─────────────────────────────────────────────────────────────────

@mcp.tool()
def create_schedule(
    cron_expr: str,
    template_id: str | None = None,
    workflow_id: str | None = None,
    params_json: str = "{}",
) -> dict:
    """Schedule a recurring ComfyUI workflow run using a cron expression (e.g. '0 9 * * *' = 9am daily). CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix."""
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in params_json: {e}"}
    body: dict[str, Any] = {"cron_expr": cron_expr, "params": params}
    if template_id:
        body["template_id"] = template_id
    workflow_id = _sanitize_workflow_id(workflow_id)
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/schedules", body)


@mcp.tool()
def list_schedules() -> dict:
    """List orchestration workflow schedules (ComfyUI jobs)."""
    return _get("/api/orchestration/schedules")


@mcp.tool()
def update_schedule(schedule_id: str, enabled: bool | None = None, cron_expr: str | None = None) -> dict:
    """Enable/disable a schedule or change its cron expression. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    body: dict[str, Any] = {}
    if enabled is not None:
        body["enabled"] = enabled
    if cron_expr is not None:
        body["cron_expr"] = cron_expr
    return _patch(f"/api/orchestration/schedules/{schedule_id}", body)


@mcp.tool()
def delete_schedule(schedule_id: str) -> dict:
    """Remove a schedule permanently. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _delete(f"/api/orchestration/schedules/{schedule_id}")


# ── ComfyUI ops ───────────────────────────────────────────────────────────────

@mcp.tool()
def restart_comfyui(confirm: bool = False) -> dict:
    """Restart ComfyUI via ops-controller (privileged). Set confirm=true to proceed."""
    if not confirm:
        return {"error": "Set confirm=true to restart the ComfyUI service."}
    return _post("/api/orchestration/comfyui/restart", {"confirm": True})


if __name__ == "__main__":
    mcp.run()