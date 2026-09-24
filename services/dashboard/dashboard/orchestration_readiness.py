"""Capability-based readiness: model-gateway (chat + MCP gateway), optional ComfyUI + workflow dir."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# The ComfyUI GPU admission gate; empty when comfyui is not enabled.
COMFYUI_URL = os.environ.get("COMFYUI_URL", "").rstrip("/")
MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435").rstrip("/")
MODEL_GATEWAY_API_KEY = os.environ.get("MODEL_GATEWAY_API_KEY", "")
WORKFLOWS_DIR = Path(os.environ.get("COMFYUI_WORKFLOWS_DIR", "/comfyui-workflows")).resolve()
ORCHESTRATION_MEDIA_REQUIRED = os.environ.get("ORCHESTRATION_MEDIA_REQUIRED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)


def _probe_get(url: str, timeout: float = 3.0) -> tuple[bool, str | None]:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
        ok = r.status_code < 500
        if r.status_code == 400 and "/mcp" in url:
            ok = True
        return ok, None if ok else f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def _probe_litellm_mcp_tools(url: str, api_key: str, timeout: float = 30.0) -> tuple[bool, int, str | None]:
    """tools/list against LiteLLM's aggregate /mcp (no initialize needed). ok when every server
    outcome is `ok` and at least one tool is listed; the per-server outcomes name the culprit."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "x-litellm-api-key": f"Bearer {api_key}"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            return False, 0, f"tools/list HTTP {r.status_code}"
        result: dict = {}
        for line in r.text.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg:
                    result = msg["result"]
        if not result and r.text.strip().startswith("{"):
            result = json.loads(r.text).get("result", {})
        tools = result.get("tools", [])
        outcomes = (result.get("_meta") or {}).get("litellm.ai/server_outcomes") or {}
        bad = sorted(k for k, v in outcomes.items() if v.get("status") != "ok")
        if bad:
            return False, len(tools), f"servers not ok: {', '.join(bad)}"
        if not tools:
            return False, 0, "tools/list returned 0 tools"
        return True, len(tools), None
    except Exception as e:  # noqa: BLE001
        return False, 0, str(e)


def compute_readiness() -> dict:
    """Return structured readiness; use ok_all for a single gate."""
    model_ok, model_err = _probe_get(f"{MODEL_GATEWAY_URL}/ready")
    mcp_ok, mcp_tool_count, mcp_err = _probe_litellm_mcp_tools(f"{MODEL_GATEWAY_URL}/mcp", MODEL_GATEWAY_API_KEY)

    media_ok = True
    media_err: str | None = None
    if ORCHESTRATION_MEDIA_REQUIRED:
        u_ok, u_err = (_probe_get(f"{COMFYUI_URL}/") if COMFYUI_URL
                       else (False, "COMFYUI_URL is not set (comfyui is not enabled)"))
        if not u_ok:
            media_ok = False
            media_err = u_err
        elif not WORKFLOWS_DIR.is_dir():
            media_ok = False
            media_err = f"workflows dir missing: {WORKFLOWS_DIR}"
        else:
            try:
                next(WORKFLOWS_DIR.rglob("*.json"), None)
            except OSError as e:
                media_ok = False
                media_err = str(e)

    ok_all = model_ok and mcp_ok
    if ORCHESTRATION_MEDIA_REQUIRED:
        ok_all = ok_all and media_ok

    checks = [
        {"id": "model_gateway_ready", "ok": model_ok, "error": model_err},
        {"id": "mcp_gateway_reachable", "ok": mcp_ok, "error": mcp_err, "tool_count": mcp_tool_count},
        {
            "id": "comfyui_media",
            "ok": media_ok,
            "required": ORCHESTRATION_MEDIA_REQUIRED,
            "error": media_err,
            "workflows_dir": str(WORKFLOWS_DIR),
        },
        {"id": "orchestration_probe", "ok": True},
    ]

    return {
        "ok": ok_all,
        "checks": checks,
    }
