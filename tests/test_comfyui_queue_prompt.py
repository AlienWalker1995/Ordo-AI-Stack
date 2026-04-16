"""queue_prompt tool — posts raw API-format workflow JSON to ComfyUI /prompt."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def mcp_app():
    """Create a FastMCP app with system tools registered."""
    import importlib.util
    import sys
    from pathlib import Path

    # Ensure comfyui-mcp package is importable
    mcp_root = Path("comfyui-mcp")
    if str(mcp_root) not in sys.path:
        sys.path.insert(0, str(mcp_root))

    mcp = FastMCP("test")
    from tools.system import register_system_tools
    register_system_tools(mcp)
    return mcp


def _get_tool_fn(mcp_app: FastMCP, name: str):
    """Extract the underlying function for a registered MCP tool by name."""
    for tool in mcp_app._tool_manager._tools.values():
        if tool.name == name:
            return tool.fn
    raise KeyError(f"Tool '{name}' not registered")


class TestQueuePrompt:
    """queue_prompt tool tests."""

    def test_accepts_valid_api_format_and_posts(self, mcp_app):
        workflow = {
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "a cat", "clip": ["4", 1]},
            },
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": 42,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"prompt_id": "abc-123"}

        fn = _get_tool_fn(mcp_app, "queue_prompt")
        with patch("tools.system.requests.post", return_value=mock_response) as mock_post:
            result = fn(workflow_json='{"6":{"class_type":"CLIPTextEncode","inputs":{"text":"a cat","clip":["4",1]}},"3":{"class_type":"KSampler","inputs":{"seed":42,"model":["4",0],"positive":["6",0],"negative":["7",0],"latent_image":["5",0]}}}')

        assert result["ok"] is True
        assert result["prompt_id"] == "abc-123"
        mock_post.assert_called_once()
        call_body = mock_post.call_args[1].get("json") or mock_post.call_args[0][1] if len(mock_post.call_args[0]) > 1 else mock_post.call_args[1]["json"]
        assert "prompt" in call_body

    def test_rejects_ui_format(self, mcp_app):
        ui_json = '{"nodes":[{"type":"CLIPTextEncode","id":1}]}'
        fn = _get_tool_fn(mcp_app, "queue_prompt")
        result = fn(workflow_json=ui_json)
        assert "error" in result
        assert "UI" in result["error"] or "editor" in result["error"]

    def test_rejects_invalid_json(self, mcp_app):
        fn = _get_tool_fn(mcp_app, "queue_prompt")
        result = fn(workflow_json="not valid json {{{")
        assert "error" in result
        assert "JSON" in result["error"]

    def test_rejects_empty_workflow(self, mcp_app):
        fn = _get_tool_fn(mcp_app, "queue_prompt")
        result = fn(workflow_json="{}")
        assert "error" in result

    def test_returns_error_on_comfyui_failure(self, mcp_app):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": "invalid prompt"}

        fn = _get_tool_fn(mcp_app, "queue_prompt")
        with patch("tools.system.requests.post", return_value=mock_response):
            result = fn(workflow_json='{"6":{"class_type":"CLIPTextEncode","inputs":{"text":"a cat","clip":["4",1]}}}')
        assert result["ok"] is False

    def test_strips_gemma_quoted_strings(self, mcp_app):
        """Gemma token-bleeding wraps values in extra quotes — queue_prompt must strip them."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"prompt_id": "sanitized-123"}

        fn = _get_tool_fn(mcp_app, "queue_prompt")
        # ckpt_name has embedded quotes like Gemma produces
        workflow_json = json.dumps({
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": '"flux1-schnell-fp8.safetensors"'},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "a cat", "clip": ["4", 1]},
            },
        })
        with patch("tools.system.requests.post", return_value=mock_response) as mock_post:
            result = fn(workflow_json=workflow_json)

        assert result["ok"] is True
        # Verify the posted workflow has clean ckpt_name (no embedded quotes)
        posted_body = mock_post.call_args[1]["json"]
        assert posted_body["prompt"]["4"]["inputs"]["ckpt_name"] == "flux1-schnell-fp8.safetensors"

    def test_strips_gemma_special_tokens(self, mcp_app):
        """Gemma <|X|> special tokens in values must be cleaned."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"prompt_id": "token-clean-456"}

        fn = _get_tool_fn(mcp_app, "queue_prompt")
        workflow_json = json.dumps({
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": '<|"|>flux1-schnell-fp8.safetensors<|"|>'},
            },
        })
        with patch("tools.system.requests.post", return_value=mock_response) as mock_post:
            result = fn(workflow_json=workflow_json)

        assert result["ok"] is True
        posted_body = mock_post.call_args[1]["json"]
        assert posted_body["prompt"]["1"]["inputs"]["ckpt_name"] == "flux1-schnell-fp8.safetensors"
