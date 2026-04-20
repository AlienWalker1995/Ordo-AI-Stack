"""Ordo AI Stack: ComfyUI stack management tools (MCP) — ops-controller for pip + restart.

MCP clients use the same paradigm as other MCP tools: gateway__call with
inner tool names install_custom_node_requirements / restart_comfyui (or flat gateway__comfyui__*).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("MCP_Server")

OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()


def _ops_get(path: str, timeout: int = 60) -> dict:
    """GET from ops-controller (Bearer token)."""
    if not OPS_CONTROLLER_TOKEN:
        return {
            "ok": False,
            "error": (
                "OPS_CONTROLLER_TOKEN is not set on the ComfyUI MCP server. "
                "Set it in .env and pass it through mcp/gateway/registry-custom.yaml (catalog fragment for mcp-gateway --additional-catalog)."
            ),
        }
    url = f"{OPS_CONTROLLER_URL}{path}"
    headers = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        try:
            data = r.json()
        except (ValueError, UnicodeDecodeError):
            data = {"detail": r.text}
        if not isinstance(data, dict):
            data = {"detail": data}
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, **data}
        return {"ok": True, **data}
    except requests.RequestException as e:
        logger.warning("ops-controller GET failed: %s", e)
        return {"ok": False, "error": str(e)}


def _ops_post(path: str, body: dict[str, Any], timeout: int = 600) -> dict:
    if not OPS_CONTROLLER_TOKEN:
        return {
            "ok": False,
            "error": (
                "OPS_CONTROLLER_TOKEN is not set on the ComfyUI MCP server. "
                "Set it in .env and pass it through mcp/gateway/registry-custom.yaml (and mcp-gateway env; --additional-catalog) "
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
        except (ValueError, UnicodeDecodeError):
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

    @mcp.tool()
    def list_comfyui_model_packs() -> dict:
        """List downloadable ComfyUI model pack IDs, descriptions, and per-pack file counts (from scripts/comfyui/models.json on the host). Use before pull_comfyui_models."""
        return _ops_get("/models/packs")

    @mcp.tool()
    def pull_comfyui_models(packs: str, confirm: bool = True) -> dict:
        """Download HuggingFace weights into the shared ComfyUI models volume (same job as the dashboard / comfyui-model-puller). Runs in the background; poll get_comfyui_model_pull_status.

        Args:
            packs: One or more comma-separated pack IDs, e.g. "ltx-2.3-t2v-basic,ltx-2.3-extras".
                For Kijai/LTX-2.3 Basic-style workflows (CLIPLoader Gemma+LTX, distilled UNET under LTXVideo/v2, KJ VAEs, taeltx preview), use at least ltx-2.3-t2v-basic; add ltx-2.3-extras for the spatial latent upscaler file.
            confirm: Must be true to start the download.

        Requires: OPS_CONTROLLER_TOKEN, ops-controller reachable, HF_TOKEN in repo .env for gated repos.
        """
        if not confirm:
            return {"ok": False, "error": "confirm must be true to execute"}
        p = (packs or "").strip()
        if not p:
            return {"ok": False, "error": "packs is required (e.g. ltx-2.3-t2v-basic,ltx-2.3-extras)"}
        return _ops_post("/models/pull", {"pack": p, "confirm": True}, timeout=120)

    @mcp.tool()
    def get_comfyui_model_pull_status() -> dict:
        """Poll the last pull_comfyui_models run: running, done, success, output log, pack string."""
        return _ops_get("/models/pull/status")

    @mcp.tool()
    def download_comfyui_model(url: str, category: str = "", filename: str = "") -> dict:
        """Download any model file from HTTPS URL into ComfyUI's models directory.

        Use this for models NOT in the curated packs — e.g. from HuggingFace, GitHub releases,
        CivitAI, or any direct download URL.

        Args:
            url: Direct HTTPS download URL to the model file (e.g.
                "https://huggingface.co/org/repo/resolve/main/model.safetensors").
                Must start with https://.
            category: Target subfolder under ComfyUI models directory. One of:
                checkpoints, loras, text_encoders, vae, unet, clip, clip_vision,
                controlnet, embeddings, upscale_models, diffusion_models,
                latent_upscale_models, vae_approx.
                If omitted, auto-detected from URL/filename keywords.
            filename: Override filename to save as. If omitted, extracted from URL.

        Runs in the background with resume support. Poll get_comfyui_model_download_status
        for progress. Requires HF_TOKEN in .env for gated HuggingFace repos.
        """
        u = (url or "").strip()
        if not u:
            return {"ok": False, "error": "url is required"}
        if not u.startswith("https://"):
            return {"ok": False, "error": "url must start with https://"}
        body: dict[str, Any] = {"url": u}
        if category:
            body["category"] = category.strip()
        if filename:
            body["filename"] = filename.strip()
        return _ops_post("/models/download", body, timeout=120)

    @mcp.tool()
    def get_comfyui_model_download_status() -> dict:
        """Poll the last download_comfyui_model run: running, done, success, progress %, filename, category."""
        return _ops_get("/models/download/status")

    @mcp.tool()
    def pull_comfyui_gguf_models(repos: str = "", confirm: bool = True) -> dict:
        """Download GGUF-quantized models from HuggingFace repos into ComfyUI.

        Args:
            repos: Comma-separated HuggingFace repo IDs, e.g.
                "city96/FLUX.1-dev-gguf,unsloth/LTX-2.3-GGUF".
                If empty, uses GGUF_MODELS from .env.
            confirm: Must be true to start the download.

        Runs in the background; poll get_comfyui_gguf_pull_status for progress.
        """
        if not confirm:
            return {"ok": False, "error": "confirm must be true to execute"}
        body: dict[str, Any] = {"confirm": True}
        r = (repos or "").strip()
        if r:
            body["repos"] = r
        return _ops_post("/models/gguf-pull", body, timeout=120)

    @mcp.tool()
    def get_comfyui_gguf_pull_status() -> dict:
        """Poll the last pull_comfyui_gguf_models run: running, done, success, output log, repos."""
        return _ops_get("/models/gguf-pull/status")
