"""Workflow management tools for ComfyUI MCP Server (Ordo AI Stack overlay).

Upstream: joenorton/comfyui-mcp-server — patched so `run_workflow` accepts the
flat args shape OpenClaw models often send (prompt/width/height at top level
without `workflow_id` or nested `overrides`), and optional default workflow_id
via COMFY_MCP_DEFAULT_WORKFLOW_ID.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from tools.helpers import register_and_build_response

logger = logging.getLogger("MCP_Server")

_RESERVED_RUN_KEYS = frozenset(
    {"workflow_id", "overrides", "options", "return_inline_preview"}
)


def _merge_run_workflow_args(
    workflow_id: Optional[str],
    overrides: Optional[Dict[str, Any]],
    options: Optional[Dict[str, Any]],
    return_inline_preview: bool,
    **extra: Any,
) -> tuple[str, Dict[str, Any], Optional[Dict[str, Any]], bool]:
    """Merge flat kwargs into overrides; apply default workflow_id when configured."""
    merged: Dict[str, Any] = dict(overrides or {})
    for k, v in extra.items():
        if k in _RESERVED_RUN_KEYS:
            continue
        if v is not None:
            merged[k] = v
    wid = (workflow_id or "").strip() or None
    default_wf = os.environ.get("COMFY_MCP_DEFAULT_WORKFLOW_ID", "").strip() or None
    allow_default = os.environ.get("COMFY_MCP_ALLOW_DEFAULT_WORKFLOW_ID", "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if (
        not wid
        and allow_default
        and default_wf
        and (merged.get("prompt") or merged.get("width") or merged.get("height"))
    ):
        wid = default_wf
    if not wid:
        raise ValueError(
            "workflow_id is required. Pass workflow_id (e.g. mcp-api/generate_image) or set "
            "COMFY_MCP_DEFAULT_WORKFLOW_ID when sending prompt/width in flat form."
        )
    return wid, merged, options, return_inline_preview


def register_workflow_tools(
    mcp: FastMCP,
    workflow_manager,
    comfyui_client,
    defaults_manager,
    asset_registry,
):
    """Register workflow tools with the MCP server"""

    @mcp.tool()
    def list_workflows() -> dict:
        """List all available workflows in the workflow directory.

        Returns a catalog of workflows with their IDs, names, descriptions,
        available inputs, and optional metadata.
        """
        catalog = workflow_manager.get_workflow_catalog()
        return {
            "workflows": catalog,
            "count": len(catalog),
            "workflow_dir": str(workflow_manager.workflows_dir),
        }

    @mcp.tool()
    def run_workflow(
        workflow_id: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        return_inline_preview: bool = False,
        prompt: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        seed: Optional[int] = None,
        steps: Optional[int] = None,
        cfg: Optional[float] = None,
        sampler_name: Optional[str] = None,
        scheduler: Optional[str] = None,
        denoise: Optional[float] = None,
        model: Optional[str] = None,
        negative_prompt: Optional[str] = None,
    ) -> dict:
        """Run a saved ComfyUI workflow with constrained parameter overrides.

        Args:
            workflow_id: The workflow ID (path under the workflows dir, e.g., "mcp-api/generate_image").
            overrides: Optional dict of parameter overrides (e.g., {"prompt": "a cat", "width": 1024}).
            options: Optional dict of execution options (reserved for future use)
            return_inline_preview: If True, include a small thumbnail base64 in response (256px, ~100KB)
            prompt, width, height, ...: Optional flat overrides (merged into overrides) for clients
                that omit the nested `overrides` object.

        Returns:
            Result with asset_url, workflow_id, and execution metadata. If return_inline_preview=True,
            also includes inline_preview_base64 for immediate viewing.
        """
        try:
            flat = {
                "prompt": prompt,
                "width": width,
                "height": height,
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": denoise,
                "model": model,
                "negative_prompt": negative_prompt,
            }
            wid, ov, opt, rip = _merge_run_workflow_args(
                workflow_id,
                overrides,
                options,
                return_inline_preview,
                **{k: v for k, v in flat.items() if v is not None},
            )
        except ValueError as e:
            return {"error": str(e)}

        workflow = workflow_manager.load_workflow(wid)
        if not workflow:
            return {"error": f"Workflow '{wid}' not found"}

        if workflow_manager.is_ui_workflow_export(workflow):
            return {
                "error": (
                    "This JSON is a ComfyUI UI/workflow-editor export (has a 'nodes' array). "
                    "The MCP server must send API-format graphs to /prompt. "
                    "In ComfyUI: load the workflow → Save (API format) or use 'Save API Format', "
                    "then place that file under the workflows directory and use its path as workflow_id."
                )
            }

        try:
            workflow = workflow_manager.apply_workflow_overrides(
                workflow, wid, ov, defaults_manager
            )

            override_report = workflow.pop("__override_report__", None)

            output_preferences = workflow_manager._guess_output_preferences(workflow)

            result = comfyui_client.run_custom_workflow(
                workflow,
                preferred_output_keys=output_preferences,
            )

            response = register_and_build_response(
                result,
                wid,
                asset_registry,
                tool_name=None,
                return_inline_preview=rip,
                session_id=None,
            )

            if override_report and override_report.get("overrides_dropped"):
                response["overrides_applied"] = override_report["overrides_applied"]
                response["overrides_dropped"] = override_report["overrides_dropped"]

            return response
        except Exception as exc:
            logger.exception("Workflow '%s' failed", wid)
            return {"error": str(exc)}
