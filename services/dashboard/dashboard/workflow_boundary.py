"""Reject ComfyUI UI/editor exports at HTTP boundaries; API-format graphs only."""

from __future__ import annotations

from typing import Any


def is_ui_workflow_export(workflow: dict[str, Any]) -> bool:
    """True if JSON is ComfyUI visual editor export (not /prompt API format)."""
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    n0 = nodes[0]
    return isinstance(n0, dict) and "type" in n0 and "class_type" not in n0


def assert_api_workflow(workflow: dict[str, Any], *, context: str = "workflow") -> None:
    if not isinstance(workflow, dict):
        raise ValueError(f"{context} must be a JSON object")
    if is_ui_workflow_export(workflow):
        raise ValueError(
            f"{context}: UI/editor export detected (nodes[] with type, no class_type). "
            "Use API-format JSON for /prompt (Save API Format in ComfyUI)."
        )
