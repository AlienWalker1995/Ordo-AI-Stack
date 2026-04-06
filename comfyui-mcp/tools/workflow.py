"""Workflow management tools for ComfyUI MCP Server (Ordo AI Stack overlay).

Upstream: joenorton/comfyui-mcp-server — patched so `run_workflow` accepts the
flat args shape OpenClaw models often send (prompt/width/height at top level
without `workflow_id` or nested `overrides`), and optional default workflow_id
via COMFY_MCP_DEFAULT_WORKFLOW_ID.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from tools.helpers import register_and_build_response

logger = logging.getLogger("MCP_Server")

_RESERVED_RUN_KEYS = frozenset(
    {"workflow_id", "overrides", "options", "return_inline_preview"}
)


def _merge_run_workflow_args(
    workflow_id: str | None,
    overrides: dict[str, Any] | None,
    options: dict[str, Any] | None,
    return_inline_preview: bool,
    **extra: Any,
) -> tuple[str, dict[str, Any], dict[str, Any] | None, bool]:
    """Merge flat kwargs into overrides; apply default workflow_id when configured."""
    merged: dict[str, Any] = dict(overrides or {})
    for k, v in extra.items():
        if k in _RESERVED_RUN_KEYS:
            continue
        if v is not None:
            merged[k] = v
    if merged.get("style_prompt") is not None and merged.get("tags") is None:
        merged["tags"] = merged["style_prompt"]
    elif merged.get("tags") is not None and merged.get("style_prompt") is None:
        merged["style_prompt"] = merged["tags"]
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
    def list_workflows(details: bool = False) -> dict:
        """List workflows under the ComfyUI user workflows directory.

        Default (details=false) returns every workflow_id in a compact list so large trees
        are not truncated by MCP/LLM context limits. Use details=true for full per-workflow
        metadata and available_inputs (heavy).

        Many entries are ComfyUI UI/editor JSON; run_workflow requires API-format graphs
        (or sidecar .wfmeta for overrides). Prefer mcp-api/* for stack-tested API graphs.
        """
        wf_dir = str(workflow_manager.workflows_dir)
        ids = workflow_manager.list_workflow_ids()
        if not details:
            return {
                "workflow_ids": ids,
                "count": len(ids),
                "workflow_dir": wf_dir,
                "note": "Compact list. Pass details=true for full catalog. UI-format JSON cannot run via run_workflow.",
            }
        catalog = workflow_manager.get_workflow_catalog()
        return {
            "workflows": catalog,
            "count": len(catalog),
            "workflow_dir": wf_dir,
        }

    @mcp.tool()
    def run_workflow(
        workflow_id: str | None = None,
        overrides: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        return_inline_preview: bool = False,
        prompt: str | None = None,
        width: int | None = None,
        height: int | None = None,
        seed: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
        denoise: float | None = None,
        model: str | None = None,
        negative_prompt: str | None = None,
        tags: str | None = None,
        style_prompt: str | None = None,
        lyrics: str | None = None,
        seconds: int | None = None,
        lyrics_strength: float | None = None,
        language: str | None = None,
        key: str | None = None,
        duration: int | None = None,
        fps: int | None = None,
        frames: int | None = None,
    ) -> dict:
        """Run a saved ComfyUI workflow with constrained parameter overrides.

        Args:
            workflow_id: The workflow ID (path under the workflows dir, e.g., "mcp-api/generate_image").
            overrides: Optional dict of parameter overrides (e.g., {"prompt": "a cat", "width": 1024}).
            options: Optional dict of execution options (reserved for future use)
            return_inline_preview: If True, include a small thumbnail base64 in response (256px, ~100KB)
            prompt, width, height, ...: Optional flat overrides (merged into overrides) for clients
                that omit the nested `overrides` object. This includes image, audio, and video
                parameters such as tags, lyrics, seconds, lyrics_strength, duration, fps, and frames.
                `style_prompt` is accepted as a clearer alias for `tags`.

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
                "tags": tags,
                "style_prompt": style_prompt,
                "lyrics": lyrics,
                "seconds": seconds,
                "lyrics_strength": lyrics_strength,
                "language": language,
                "key": key,
                "duration": duration,
                "fps": fps,
                "frames": frames,
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
