"""AI-toolkit: ComfyUI stack management tools (MCP) — ops-controller for pip + restart.

OpenClaw and other clients use the same paradigm as other MCP tools: gateway__call with
inner tool names install_custom_node_requirements / restart_comfyui (or flat gateway__comfyui__*).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("MCP_Server")

OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()


def _ops_post(path: str, body: Dict[str, Any], timeout: int = 600) -> dict:
    if not OPS_CONTROLLER_TOKEN:
        return {
            "ok": False,
            "error": (
                "OPS_CONTROLLER_TOKEN is not set on the ComfyUI MCP server. "
                "Set it in .env and pass it through mcp/registry-custom.yaml (and mcp-gateway env) "
                "so spawned MCP containers can reach ops-controller."
            ),
        }
    url = f"{OPS_CONTROLLER_URL}{path}"
    headers = {
        "Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = {"detail": r.text}
        if not isinstance(data, dict):
            data = {"detail": data}
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, **data}
        return {"ok": True, **data}
    except requests.RequestException as e:
        logger.warning("ops-controller request failed: %s", e)
        return {"ok": False, "error": str(e)}


def register_management_tools(mcp: FastMCP) -> None:
    """Register install/restart tools (same surface as other ComfyUI MCP tools)."""

    @mcp.tool()
    def install_custom_node_requirements(node_path: str, confirm: bool = True) -> dict:
        """Run pip install -r requirements.txt inside the comfyui container for a custom_nodes subfolder.

        Args:
            node_path: Path under ComfyUI custom_nodes (e.g. juno-comfyui-nodes-main). Must contain requirements.txt on the shared host volume.
            confirm: Must be true to execute (safety).

        Requires: comfyui service running; ops-controller with OPS_CONTROLLER_TOKEN; files already under data/comfyui-storage/ComfyUI/custom_nodes/.
        """
        if not confirm:
            return {"ok": False, "error": "confirm must be true to execute"}
        np = (node_path or "").strip()
        if not np:
            return {"ok": False, "error": "node_path is required"}
        return _ops_post(
            "/comfyui/install-node-requirements",
            {"node_path": np, "confirm": True},
            timeout=600,
        )

    @mcp.tool()
    def restart_comfyui(confirm: bool = True) -> dict:
        """Restart the comfyui Docker service so new custom nodes and Python deps are picked up.

        Args:
            confirm: Must be true to execute (safety).

        Requires: ops-controller with OPS_CONTROLLER_TOKEN (same as dashboard service controls).
        """
        if not confirm:
            return {"ok": False, "error": "confirm must be true to execute"}
        return _ops_post("/services/comfyui/restart", {"confirm": True}, timeout=120)
