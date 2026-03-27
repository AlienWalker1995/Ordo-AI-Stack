"""Capability-based readiness: MCP gateway, model-gateway, optional ComfyUI + workflow dir."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435").rstrip("/")
MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "http://mcp-gateway:8811").rstrip("/")
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


def compute_readiness() -> dict:
    """Return structured readiness; use ok_all for a single gate."""
    model_ok, model_err = _probe_get(f"{MODEL_GATEWAY_URL}/ready")
    mcp_ok, mcp_err = _probe_get(f"{MCP_GATEWAY_URL}/mcp")

    media_ok = True
    media_err: str | None = None
    if ORCHESTRATION_MEDIA_REQUIRED:
        u_ok, u_err = _probe_get(f"{COMFYUI_URL}/")
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
        {"id": "mcp_gateway_reachable", "ok": mcp_ok, "error": mcp_err},
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
