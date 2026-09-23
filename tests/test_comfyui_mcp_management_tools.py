"""The ComfyUI MCP management tools the agent sees: only ones that can work."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Same as the other comfyui-mcp tests: fastmcp is a service dependency, not a CI test dependency.
pytest.importorskip(
    "mcp.server.fastmcp",
    reason="local-only: mcp/fastmcp is a comfyui-mcp service dependency, deliberately not installed in CI",
)
from mcp.server.fastmcp import FastMCP  # noqa: E402


def _tool_names() -> set[str]:
    mcp_root = Path("services/comfyui-mcp")
    if str(mcp_root) not in sys.path:
        sys.path.insert(0, str(mcp_root))
    from tools.management import register_management_tools

    mcp = FastMCP("test")
    register_management_tools(mcp)
    return {tool.name for tool in mcp._tool_manager._tools.values()}


def test_pack_pull_tools_are_not_offered():
    # Their control-plane routes never worked on this stack (always 404/501).
    retired = {"list_comfyui_model_packs", "pull_comfyui_models", "get_comfyui_model_pull_status",
               "pull_comfyui_gguf_models", "get_comfyui_gguf_pull_status"}
    assert not (_tool_names() & retired)


def test_the_working_download_tools_remain():
    assert {"download_comfyui_model", "get_comfyui_model_download_status"} <= _tool_names()
