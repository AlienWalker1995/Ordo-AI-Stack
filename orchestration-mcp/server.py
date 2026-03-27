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


def _get(path: str) -> dict[str, Any]:
    with httpx.Client(timeout=60.0) as client:
        r = client.get(f"{BASE}{path}", headers=_headers())
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


mcp = FastMCP("orchestration")


@mcp.tool()
def orchestration_readiness() -> dict:
    """Return capability readiness (model-gateway, MCP gateway, optional ComfyUI). Public endpoint."""
    with httpx.Client(timeout=15.0) as client:
        r = client.get(f"{BASE}/api/orchestration/readiness")
        return r.json()


@mcp.tool()
def list_workflows() -> dict:
    """List typed templates and workflow API files under the ComfyUI workflows directory."""
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
    """Compile a typed template with validated parameters to API-format graph (no raw graph editing)."""
    params = json.loads(params_json) if params_json else {}
    return _post(
        "/api/orchestration/workflows/from-template",
        {"template_id": template_id, "params": params},
    )


@mcp.tool()
def run_workflow(
    template_id: str | None = None,
    workflow_id: str | None = None,
    params_json: str = "{}",
) -> dict:
    """Queue a workflow run; returns job_id. Poll await_run / publish_status for receipts."""
    params = json.loads(params_json) if params_json else {}
    body: dict[str, Any] = {"params": params}
    if template_id:
        body["template_id"] = template_id
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _post("/api/orchestration/run", body)


@mcp.tool()
def await_run(job_id: str) -> dict:
    """Get execution receipt and state for a job (queued → validated → running → artifact_ready / failed)."""
    return _get(f"/api/orchestration/jobs/{job_id}")


@mcp.tool()
def publish_enqueue(job_id: str, webhook_url: str | None = None, payload_json: str = "{}") -> dict:
    """Enqueue publish delivery to n8n (executor of record). Retries/OAuth belong in n8n, not OpenClaw."""
    payload = json.loads(payload_json) if payload_json else {}
    body: dict[str, Any] = {"job_id": job_id, "payload": payload}
    if webhook_url:
        body["webhook_url"] = webhook_url
    return _post("/api/orchestration/publish/enqueue", body)


@mcp.tool()
def publish_status(job_id: str) -> dict:
    """Publish pipeline status for a job."""
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{BASE}/api/orchestration/publish/status",
            params={"job_id": job_id},
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
def restart_comfyui(confirm: bool = False) -> dict:
    """Restart ComfyUI via ops-controller (privileged HTTP), not via ad-hoc MCP gateway names."""
    if not confirm:
        return {"error": "Set confirm=true to restart the ComfyUI service."}
    return _post("/api/orchestration/comfyui/restart", {"confirm": True})


if __name__ == "__main__":
    mcp.run()
