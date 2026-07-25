"""ComfyUI system state tools — direct HTTP queries to ComfyUI's API.

Gives MCP clients full visibility into ComfyUI state:
GPU/VRAM usage, queue status, execution history, installed models, extensions,
and node definitions.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("MCP_Server")

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")


def _comfy_get(path: str, timeout: int = 30) -> dict:
    """GET from ComfyUI HTTP API."""
    url = f"{COMFYUI_URL}{path}"
    try:
        r = requests.get(url, timeout=timeout)
        try:
            data = r.json()
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": f"Non-JSON response: {r.text[:500]}"}
        if r.status_code >= 400:
            if isinstance(data, dict):
                return {"ok": False, "status_code": r.status_code, **data}
            return {"ok": False, "status_code": r.status_code, "detail": data}
        if isinstance(data, dict):
            return {"ok": True, **data}
        return {"ok": True, "data": data}
    except requests.ConnectionError:
        return {"ok": False, "error": "ComfyUI is not reachable. Is the service running?"}
    except requests.RequestException as e:
        logger.warning("ComfyUI GET %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


def _comfy_post(path: str, body: dict[str, Any] | None = None, timeout: int = 30) -> dict:
    """POST to ComfyUI HTTP API."""
    url = f"{COMFYUI_URL}{path}"
    try:
        r = requests.post(url, json=body or {}, timeout=timeout)
        try:
            data = r.json()
        except (ValueError, UnicodeDecodeError):
            if r.status_code < 400:
                return {"ok": True, "detail": r.text[:500] if r.text else "success"}
            return {"ok": False, "error": r.text[:500]}
        if r.status_code >= 400:
            if isinstance(data, dict):
                return {"ok": False, "status_code": r.status_code, **data}
            return {"ok": False, "status_code": r.status_code, "detail": data}
        if isinstance(data, dict):
            return {"ok": True, **data}
        return {"ok": True, "data": data}
    except requests.ConnectionError:
        return {"ok": False, "error": "ComfyUI is not reachable. Is the service running?"}
    except requests.RequestException as e:
        logger.warning("ComfyUI POST %s failed: %s", path, e)
        return {"ok": False, "error": str(e)}


def register_system_tools(mcp: FastMCP) -> None:
    """Register ComfyUI system state tools."""

    @mcp.tool()
    def get_comfyui_system_stats() -> dict:
        """Get ComfyUI system stats: GPU name, VRAM total/free/used, CPU/RAM usage, torch version, CUDA version.

        Use this to check if ComfyUI is running and what hardware resources are available
        before submitting generation jobs.
        """
        return _comfy_get("/system_stats")

    @mcp.tool()
    def get_comfyui_queue() -> dict:
        """Get ComfyUI queue status: currently running prompts and pending queue.

        Returns queue_running (list of prompts being executed) and queue_pending
        (list of prompts waiting). Use this to check if ComfyUI is busy before
        submitting new work.
        """
        return _comfy_get("/queue")

    @mcp.tool()
    def get_comfyui_history(prompt_id: str | None = None, max_items: int = 20) -> dict:
        """Get ComfyUI execution history.

        Args:
            prompt_id: If provided, get details for a specific prompt execution.
                Otherwise returns recent history.
            max_items: Maximum history entries to return (default 20).

        Returns execution results including output file paths and any errors.
        """
        if prompt_id:
            return _comfy_get(f"/history/{prompt_id}")
        result = _comfy_get(f"/history?max_items={max_items}")
        if result.get("ok") and isinstance(result.get("data"), dict):
            # Trim to max_items if needed
            entries = result["data"]
            if len(entries) > max_items:
                keys = list(entries.keys())[:max_items]
                result["data"] = {k: entries[k] for k in keys}
            result["count"] = len(result.get("data", {}))
        return result

    @mcp.tool()
    def get_comfyui_models(folder: str = "checkpoints") -> dict:
        """List available models in a ComfyUI model folder.

        Args:
            folder: Model folder to list. Common values:
                - checkpoints: Main model files (SD, SDXL, LTX, etc.)
                - loras: LoRA adapter files
                - vae: VAE model files
                - clip: CLIP text encoder files
                - unet: UNET/diffusion model files
                - embeddings: Textual inversion embeddings
                - controlnet: ControlNet model files
                - upscale_models: Upscaler model files
                - clip_vision: CLIP vision encoder files
                - diffusion_models: Diffusion transformer files

        Returns list of model filenames available in the specified folder.
        """
        return _comfy_get(f"/models/{folder}")

    @mcp.tool()
    def get_comfyui_extensions() -> dict:
        """List installed ComfyUI extensions/custom nodes.

        Returns list of extension URLs and paths. Use this to check which custom
        nodes are available before attempting to run workflows that depend on them.
        """
        return _comfy_get("/extensions")

    @mcp.tool()
    def get_comfyui_node_info(node_class: str | None = None) -> dict:
        """Get ComfyUI node definitions — inputs, outputs, and parameter types.

        Args:
            node_class: Specific node class name (e.g. 'KSampler', 'CLIPTextEncode',
                'EmptyLTXVLatentVideo'). If omitted, returns ALL node definitions
                (warning: very large response, use sparingly).

        Returns node input specs, output types, and widget configurations.
        Use this to verify node availability and correct parameter names
        when building or debugging workflows.
        """
        if node_class:
            return _comfy_get(f"/object_info/{node_class}")
        return _comfy_get("/object_info")

    @mcp.tool()
    def get_comfyui_embeddings() -> dict:
        """List available textual inversion embeddings.

        Returns list of embedding names that can be used in prompts
        via the embedding:name syntax.
        """
        return _comfy_get("/embeddings")

    @mcp.tool()
    def interrupt_comfyui() -> dict:
        """Interrupt/cancel the currently running ComfyUI generation.

        Stops the active prompt execution. Does not affect queued prompts.
        Use get_comfyui_queue to check what's running before interrupting.
        """
        return _comfy_post("/interrupt")

    @mcp.tool()
    def free_comfyui_vram(unload_models: bool = False, free_memory: bool = True) -> dict:
        """Free ComfyUI GPU VRAM and system memory.

        Args:
            unload_models: If true, unload all loaded models from VRAM.
            free_memory: If true, free cached memory (default).

        Use this when VRAM is tight before starting a large generation,
        or after a failed generation to reclaim resources.
        """
        body: dict[str, Any] = {}
        if unload_models:
            body["unload_models"] = True
        if free_memory:
            body["free_memory"] = True
        return _comfy_post("/free", body)

    @mcp.tool()
    def queue_prompt(workflow_json: str) -> dict:
        """Submit a raw API-format ComfyUI workflow graph for execution.

        Use this to run a workflow you composed from scratch — no saved file needed.
        The workflow must be API-format JSON (nodes keyed by ID string, each with
        class_type and inputs). UI/editor-format exports (with a top-level "nodes"
        array) are rejected.

        Build workflows using get_comfyui_node_info to discover node class names and
        their required inputs, and get_comfyui_models to find available checkpoints.

        Args:
            workflow_json: API-format workflow graph as a JSON string.
                Each key is a node ID (e.g. "3"), each value has "class_type" and "inputs".
                Example: {"3": {"class_type": "KSampler", "inputs": {...}}}

        Returns:
            prompt_id on success (use get_comfyui_history to poll for results).
        """
        try:
            workflow = json.loads(workflow_json)
        except (json.JSONDecodeError, TypeError) as e:
            return {"error": f"Invalid JSON: {e}"}

        if not isinstance(workflow, dict) or not workflow:
            return {"error": "Workflow must be a non-empty JSON object."}

        # Reject UI/editor exports
        nodes = workflow.get("nodes")
        if isinstance(nodes, list) and nodes:
            n0 = nodes[0]
            if isinstance(n0, dict) and "type" in n0 and "class_type" not in n0:
                return {
                    "error": (
                        "This is a ComfyUI UI/editor export (has 'nodes' array with 'type'). "
                        "queue_prompt requires API-format JSON where keys are node IDs and "
                        "each node has 'class_type'. Use get_comfyui_node_info to build "
                        "the correct format."
                    )
                }

        # Basic structural validation — at least one node must have class_type
        has_class_type = any(
            isinstance(v, dict) and "class_type" in v
            for v in workflow.values()
        )
        if not has_class_type:
            return {
                "error": (
                    "No nodes with 'class_type' found. Each node must have "
                    "'class_type' and 'inputs'. Use get_comfyui_node_info to discover "
                    "valid node class names."
                )
            }

        # Sanitize Gemma token-bleeding: strip <|X|> special tokens and
        # balanced surrounding quotes from string values in node inputs.
        # Gemma 4 leaks turn-separator tokens into tool-call strings, producing
        # values like '"flux1-schnell-fp8.safetensors"' (with embedded quotes)
        # or '<|"|>flux1-schnell<|"|>' which ComfyUI rejects.
        import re
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for key, val in inputs.items():
                if not isinstance(val, str):
                    continue
                cleaned = re.sub(r"<\|(.)\|>", r"\1", val).strip()
                if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'", "`"):
                    cleaned = cleaned[1:-1].strip()
                if cleaned != val:
                    inputs[key] = cleaned

        return _comfy_post("/prompt", {"prompt": workflow}, timeout=30)
