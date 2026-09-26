"""The comfyui-mcp image overlays our files onto an upstream checkout cloned at build time
(services/comfyui-mcp/Dockerfile: git clone joenorton/comfyui-mcp-server). Upstream code that is
NOT in this repo reads names our overlay provides, so a repo-only zero-caller check calls them dead.

#276 removed `WorkflowManager.tool_definitions` on exactly that reasoning; upstream
tools/generation.py reads it at startup and the MCP server crash-looped
(AttributeError: 'WorkflowManager' object has no attribute 'tool_definitions').
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "services" / "comfyui-mcp" / "managers" / "workflow_manager.py"

# Names the upstream (build-time cloned) modules read from our overlaid WorkflowManager.
UPSTREAM_READS = {"tool_definitions"}


def _instance_attributes() -> set[str]:
    tree = ast.parse(MANAGER.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        ):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
    return names


def test_the_overlay_keeps_every_name_upstream_reads():
    assert UPSTREAM_READS - _instance_attributes() == set()
