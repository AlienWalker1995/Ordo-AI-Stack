"""Integration: queue_prompt is registered and callable through the MCP tool interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# `mcp` is a comfyui-mcp service dep, not part of tests/requirements.txt.
pytest.importorskip("mcp.server.fastmcp")


def test_queue_prompt_registered_in_system_tools():
    """queue_prompt must appear in the tool list after register_system_tools."""
    mcp_root = Path("comfyui-mcp")
    if str(mcp_root) not in sys.path:
        sys.path.insert(0, str(mcp_root))

    from mcp.server.fastmcp import FastMCP
    from tools.system import register_system_tools

    mcp = FastMCP("integration-test")
    register_system_tools(mcp)

    tool_names = [t.name for t in mcp._tool_manager._tools.values()]
    assert "queue_prompt" in tool_names, f"queue_prompt not found in: {tool_names}"


def test_queue_prompt_minimal_flux_workflow():
    """A minimal Flux txt2img workflow structure passes validation."""
    mcp_root = Path("comfyui-mcp")
    if str(mcp_root) not in sys.path:
        sys.path.insert(0, str(mcp_root))

    from mcp.server.fastmcp import FastMCP
    from tools.system import register_system_tools

    mcp = FastMCP("integration-test")
    register_system_tools(mcp)

    # Find queue_prompt
    fn = None
    for tool in mcp._tool_manager._tools.values():
        if tool.name == "queue_prompt":
            fn = tool.fn
            break
    assert fn is not None

    # Minimal Flux-style workflow (just structural validation, not execution)
    workflow = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "flux1-schnell-fp8.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "a beautiful sunset", "clip": ["1", 1]},
        },
        "3": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
        },
        "4": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["2", 0],
                "latent_image": ["3", 0],
            },
        },
        "5": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["4", 0], "vae": ["1", 2]},
        },
        "6": {
            "class_type": "SaveImage",
            "inputs": {"images": ["5", 0], "filename_prefix": "queue_prompt_test"},
        },
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"prompt_id": "test-id-123"}

    with patch("tools.system.requests.post", return_value=mock_response):
        result = fn(workflow_json=json.dumps(workflow))

    assert result["ok"] is True
    assert result["prompt_id"] == "test-id-123"
