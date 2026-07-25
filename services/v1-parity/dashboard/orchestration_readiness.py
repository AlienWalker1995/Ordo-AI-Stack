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


def _probe_mcp_tools(url: str, timeout: float = 5.0) -> tuple[bool, int, str | None]:
    """Open an MCP session (initialize → tools/list) and return (ok, tool_count, error).

    The MCP Streamable HTTP transport requires a session handshake before
    accepting method calls like tools/list.
    """
    init_body = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "readiness-probe", "version": "1.0.0"},
        },
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    session_id: str | None = None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            # Step 1: initialize session
            init_r = client.post(url, json=init_body, headers=headers)
            if init_r.status_code >= 400:
                return False, 0, f"initialize HTTP {init_r.status_code}"
            session_id = init_r.headers.get("mcp-session-id")
            sess_headers = {**headers}
            if session_id:
                sess_headers["Mcp-Session-Id"] = session_id

            # Step 2: send initialized notification
            client.post(
                url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=sess_headers,
            )

            # Step 3: tools/list
            tools_r = client.post(
                url,
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 2},
                headers=sess_headers,
            )
            if tools_r.status_code >= 400:
                return False, 0, f"tools/list HTTP {tools_r.status_code}"

            # Parse SSE if the response is event-stream
            body_text = tools_r.text
            if body_text.startswith("event:") or body_text.startswith("data:"):
                data_parts = []
                for line in body_text.splitlines():
                    if line.startswith("data: "):
                        data_parts.append(line[6:])
                if data_parts:
                    body_text = "\n".join(data_parts)

            import json as _json
            data = _json.loads(body_text)
            tools = data.get("result", {}).get("tools", [])
            count = len(tools)

            # Step 4: terminate session (best-effort)
            if session_id:
                try:
                    client.request("DELETE", url, headers={"Mcp-Session-Id": session_id})
                except (httpx.RequestError, httpx.HTTPStatusError):
                    pass

            if count == 0:
                return False, 0, "tools/list returned 0 tools"
            return True, count, None
    except Exception as e:
        return False, 0, str(e)


def compute_readiness() -> dict:
    """Return structured readiness; use ok_all for a single gate."""
    model_ok, model_err = _probe_get(f"{MODEL_GATEWAY_URL}/ready")
    mcp_ok, mcp_err = _probe_get(f"{MCP_GATEWAY_URL}/mcp")

    # Verify the MCP gateway has actually loaded tools (not just responding).
    mcp_tools_ok, mcp_tool_count, mcp_tools_err = _probe_mcp_tools(f"{MCP_GATEWAY_URL}/mcp")
    if mcp_ok and not mcp_tools_ok:
        # Gateway reachable but tools not loaded yet — not ready.
        mcp_ok = False
        mcp_err = mcp_tools_err

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
