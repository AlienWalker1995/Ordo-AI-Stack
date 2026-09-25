#!/usr/bin/env python3
"""MCP adapter with stable tool names; delegates to dashboard /api/orchestration (HTTP control plane).

NOTE: the render/publish JOB worker was retired (see CHANGELOG). The job-execution, publish, and
schedule tools that used to live here were removed with it — those dashboard endpoints now return
410 Gone. What remains are the worker-INDEPENDENT verbs: workflow authoring/versioning, outputs,
readiness, the ComfyUI status/restart pair (used for fault recovery), and the model registry / GPU
views. Media generation itself runs via Hermes cron + the direct render_publish scripts.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from secret_env import read_secret

BASE = os.environ.get("ORCHESTRATION_DASHBOARD_URL", "http://dashboard:8080").rstrip("/")
# The dashboard refuses anonymous internal callers on /api/orchestration/* and every mutation;
# this adapter authenticates with the ops-controller bearer it is scoped to (plugin.yaml secrets).
# A file under /run/secrets (OPS_CONTROLLER_TOKEN_FILE, the rendered delivery), else the env var.
TOKEN = read_secret("OPS_CONTROLLER_TOKEN")


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


def _post(path: str, body: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{BASE}{path}", headers=_headers(), json=body)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except (ValueError, UnicodeDecodeError):
                detail = {"detail": r.text}
            raise RuntimeError(json.dumps(detail))
        return r.json()


# Stateless streamable HTTP on 0.0.0.0:9000 (/mcp). LiteLLM's outbound MCP client opens a fresh
# session per operation (BerriAI/litellm #25128), so the server must not depend on session state.
mcp = FastMCP("orchestration", host="0.0.0.0", port=9000, stateless_http=True)


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
    """List available typed templates (generate_image, generate_video, etc.) that can be used with create_from_template. Use this dedicated tool instead of generic gateway call tools for this operation."""
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
    return _post(f"/api/orchestration/workflows/{workflow_id}/diff?v1={v1}&v2={v2}", {})


@mcp.tool()
def promote_workflow(workflow_id: str, version: int) -> dict:
    """Mark a workflow version as the active promoted version. CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/promote?version={version}", {})


@mcp.tool()
def rollback_workflow(workflow_id: str, to_version: int) -> dict:
    """Create a new version by copying an older version (rollback). CRITICAL: Provide the raw ID only. Do NOT include the 'gateway__' prefix or any other namespace prefix inside the arguments of this tool."""
    return _post(f"/api/orchestration/workflows/{workflow_id}/rollback?to_version={to_version}", {})


# ── Outputs ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_outputs() -> dict:
    """List generated ComfyUI output files via the API (no filesystem mount required). Use this dedicated tool instead of generic gateway call tools for this operation."""
    return _get("/api/orchestration/outputs")


# ── ComfyUI ops ───────────────────────────────────────────────────────────────

@mcp.tool()
def comfyui_status() -> dict:
    """Check whether ComfyUI is up (container state + render-queue reachability).

    Use this to verify ComfyUI before/after restart_comfyui. It is ComfyUI-
    INDEPENDENT — it goes through the dashboard→ops-controller control plane,
    which stays reachable even when ComfyUI itself is down — so prefer it over
    issuing raw HTTP to guessed paths like /api/comfyui/status.
    Returns {service, container_state, queue, up}.
    """
    return _get("/api/orchestration/comfyui/status")


@mcp.tool()
def restart_comfyui(confirm: bool = False) -> dict:
    """Restart ComfyUI via ops-controller (privileged). Set confirm=true to proceed.

    Repeated calls are debounced server-side (collapsed into one in-flight
    restart), so a retry does not stack overlapping restarts. Poll comfyui_status
    afterwards instead of re-calling this in a tight loop.
    """
    if not confirm:
        return {"error": "Set confirm=true to restart the ComfyUI service."}
    return _post("/api/orchestration/comfyui/restart", {"confirm": True})


# ── Registry parity verbs ─────────────────────────────────────────────────────

@mcp.tool()
def list_models() -> dict:
    """Every model the current render serves: id, kind, service, source.file, gpu_uuid, config (ctx, mmproj)."""
    return _get("/api/orchestration/registry/models")


@mcp.tool()
def gpu_status() -> dict:
    """Live GPU VRAM/util + which models the render pins to each GPU."""
    return _get("/api/orchestration/registry/gpus")


@mcp.tool()
def list_model_catalog() -> dict:
    """The chat models this stack can switch to: gpu/cpu/embed slots (what each server has
    loaded), the catalog (id, file, installed, active) and the model files on disk (in_use)."""
    return _get("/api/models")


@mcp.tool()
def set_active_model(model_id: str, confirm: bool = False) -> dict:
    """Switch the GPU chat model to a catalog entry (an `id` from list_model_catalog whose
    `installed` is true). The source is updated and re-rendered, then llama.cpp and the gateway
    are recreated, so the switch survives the next render and carries the entry's sampler,
    projector and context settings. Chat is unavailable on the GPU for about a minute while the
    new model loads. confirm=true required."""
    if not confirm:
        return {"error": "Set confirm=true to switch the chat model (restarts llama.cpp and the gateway)."}
    return _post("/api/models/switch", {"model": model_id}, timeout=900.0)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
