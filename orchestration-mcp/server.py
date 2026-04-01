#!/usr/bin/env python3
"""MCP adapter with stable tool names; delegates to dashboard /api/orchestration (HTTP control plane)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("ORCHESTRATION_DASHBOARD_URL", "http://dashboard:8080").rstrip("/")
TOKEN = os.environ.get("DASHBOARD_AUTH_TOKEN", "").strip()


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
            except Exception:
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


def _patch(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.patch(f"{BASE}{path}", headers=_headers(), json=body)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


def _delete(path: str) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        r = client.delete(f"{BASE}{path}", headers=_headers())
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
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
def list_workflows() -> dict:
    """List typed templates and workflow API files."""
    return _get("/api/orchestration/workflows")


@mcp.tool()
def validate_workflow(workflow_json: str | None = None, workflow_id: str | None = None) -> dict:
    """Validate API-format workflow JSON; rejects ComfyUI UI/editor exports."""
    body: dict[str, Any] = {}
    if workflow_json:
        body["workflow"] = json.loads(workflow_json)
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/validate", body)


@mcp.tool()
def create_from_template(template_id: str, params_json: str = "{}") -> dict:
    """Compile a typed template to API-format graph."""
    params = json.loads(params_json) if params_json else {}
    return _post("/api/orchestration/workflows/from-template",
                 {"template_id": template_id, "params": params})


@mcp.tool()
def save_workflow(workflow_id: str, workflow_json: str, params_schema_json: str = "{}") -> dict:
    """Save a compiled API-format workflow as a new versioned snapshot."""
    workflow = json.loads(workflow_json)
    params_schema = json.loads(params_schema_json) if params_schema_json else None
    return _post("/api/orchestration/workflows/save",
                 {"workflow_id": workflow_id, "workflow": workflow, "params_schema": params_schema})


@mcp.tool()
def list_workflow_versions(workflow_id: str) -> dict:
    """List all saved versions of a workflow."""
    return _get(f"/api/orchestration/workflows/{workflow_id}/versions")


@mcp.tool()
def diff_workflow_versions(workflow_id: str, v1: int, v2: int) -> dict:
    """Unified diff between two saved workflow versions."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/diff", {"v1": v1, "v2": v2})


@mcp.tool()
def promote_workflow(workflow_id: str, version: int) -> dict:
    """Mark a workflow version as the active promoted version."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/promote?version={version}", {})


@mcp.tool()
def rollback_workflow(workflow_id: str, to_version: int) -> dict:
    """Create a new version by copying an older version (rollback)."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/rollback?to_version={to_version}", {})


# ── Job execution ─────────────────────────────────────────────────────────────

@mcp.tool()
def run_workflow(
    template_id: str | None = None,
    workflow_id: str | None = None,
    params_json: str = "{}",
) -> dict:
    """Queue a workflow run via the worker. Returns job_id immediately."""
    params = json.loads(params_json) if params_json else {}
    body: dict[str, Any] = {"params": params}
    if template_id:
        body["template_id"] = template_id
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/run", body)


@mcp.tool()
def await_run(job_id: str) -> dict:
    """Get execution receipt and current state for a job."""
    return _get(f"/api/orchestration/jobs/{job_id}")


@mcp.tool()
def list_jobs(state: str | None = None, limit: int = 20) -> dict:
    """List recent jobs, optionally filtered by state."""
    params: dict[str, Any] = {"limit": limit}
    if state:
        params["state"] = state
    return _get("/api/orchestration/jobs", params=params)


@mcp.tool()
def cancel_run(job_id: str) -> dict:
    """Request cancellation of a queued or validated job."""
    return _post(f"/api/orchestration/jobs/{job_id}/cancel", {})


# ── Publish pipeline ──────────────────────────────────────────────────────────

@mcp.tool()
def publish_enqueue(job_id: str, webhook_url: str | None = None, payload_json: str = "{}") -> dict:
    """Write to durable publish outbox (worker delivers with retries to n8n)."""
    payload = json.loads(payload_json) if payload_json else {}
    body: dict[str, Any] = {"job_id": job_id, "payload": payload}
    if webhook_url:
        body["webhook_url"] = webhook_url
    return _post("/api/orchestration/publish/enqueue", body)


@mcp.tool()
def publish_status(job_id: str) -> dict:
    """Publish pipeline status and delivery history for a job."""
    return _get("/api/orchestration/publish/status", params={"job_id": job_id})


# ── Outputs ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_outputs() -> dict:
    """List generated ComfyUI output files via the API (no filesystem mount required)."""
    return _get("/api/orchestration/outputs")


# ── Schedules ─────────────────────────────────────────────────────────────────

@mcp.tool()
def create_schedule(
    cron_expr: str,
    template_id: str | None = None,
    workflow_id: str | None = None,
    params_json: str = "{}",
) -> dict:
    """Schedule a recurring workflow run using a cron expression (e.g. '0 9 * * *' = 9am daily)."""
    params = json.loads(params_json) if params_json else {}
    body: dict[str, Any] = {"cron_expr": cron_expr, "params": params}
    if template_id:
        body["template_id"] = template_id
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/schedules", body)


@mcp.tool()
def list_schedules() -> dict:
    """List all configured schedules."""
    return _get("/api/orchestration/schedules")


@mcp.tool()
def update_schedule(schedule_id: str, enabled: bool | None = None, cron_expr: str | None = None) -> dict:
    """Enable/disable a schedule or change its cron expression."""
    body: dict[str, Any] = {}
    if enabled is not None:
        body["enabled"] = enabled
    if cron_expr is not None:
        body["cron_expr"] = cron_expr
    return _patch(f"/api/orchestration/schedules/{schedule_id}", body)


@mcp.tool()
def delete_schedule(schedule_id: str) -> dict:
    """Remove a schedule permanently."""
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
