# ComfyUI queue_prompt Tool — Let OpenClaw Build & Run Workflows

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give OpenClaw the ability to compose arbitrary ComfyUI workflow graphs from scratch and submit them directly, instead of depending on pre-saved template files.

**Architecture:** Add a `queue_prompt` MCP tool to `comfyui-mcp/tools/system.py` that accepts raw API-format workflow JSON, validates it isn't a UI export, and POSTs it to ComfyUI's `/prompt` endpoint. Fix the hardcoded default model fallback (`v1-5-pruned-emaonly.ckpt`) to auto-detect from available checkpoints. Update OpenClaw's TOOLS.md and AGENTS.md to teach the agent the compose-then-queue workflow.

**Tech Stack:** Python 3.12, FastMCP, requests, pytest, ComfyUI HTTP API

---

### Task 1: Add `queue_prompt` tool — failing test

**Files:**
- Create: `tests/test_comfyui_queue_prompt.py`

- [ ] **Step 1: Write the failing test for queue_prompt happy path**

```python
"""queue_prompt tool — posts raw API-format workflow JSON to ComfyUI /prompt."""

from __future__ import annotations

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_queue_prompt.py -v`
Expected: FAIL — `queue_prompt` tool not found in registered tools.

- [ ] **Step 3: Commit test**

```bash
git add tests/test_comfyui_queue_prompt.py
git commit -m "test: add queue_prompt tool tests (red)"
```

---

### Task 2: Implement `queue_prompt` tool

**Files:**
- Modify: `comfyui-mcp/tools/system.py:70-197` (add tool inside `register_system_tools`)

- [ ] **Step 1: Add `queue_prompt` to system.py**

Add this tool at the end of `register_system_tools`, after `free_comfyui_vram`:

```python
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

        return _comfy_post("/prompt", {"prompt": workflow}, timeout=30)
```

- [ ] **Step 2: Add the json import at the top of system.py**

Add `import json` to the imports block (after `import os`):

```python
import json
```

- [ ] **Step 3: Run the tests**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_queue_prompt.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 4: Commit**

```bash
git add comfyui-mcp/tools/system.py
git commit -m "feat(comfyui-mcp): add queue_prompt tool for raw workflow submission"
```

---

### Task 3: Fix hardcoded default model fallback

The `OPTIONAL_PARAM_DEFAULTS` dict hardcodes `"model": "v1-5-pruned-emaonly.ckpt"` which doesn't exist in most environments. Make it read from `COMFY_MCP_DEFAULT_MODEL` env var, falling back to the first available checkpoint at startup.

**Files:**
- Modify: `comfyui-mcp/managers/workflow_manager.py:52-55`
- Modify: `comfyui-mcp/managers/workflow_manager.py:35`
- Create: `tests/test_comfyui_default_model_env.py`

- [ ] **Step 1: Write failing test for env-based default model**

```python
"""Default model resolution: env var > auto-detect > hardcoded fallback."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch


@dataclass
class _WorkflowParameter:
    name: str
    placeholder: str
    annotation: type
    description: str
    required: bool
    bindings: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _WorkflowToolDefinition:
    workflow_id: str
    tool_name: str
    description: str
    template: dict
    parameters: dict
    output_preferences: tuple[str, ...]


def _load_module(env_override: dict | None = None):
    """Load workflow_manager module with optional env overrides."""
    models_pkg = type(sys)("models")
    workflow_mod = type(sys)("models.workflow")
    workflow_mod.WorkflowParameter = _WorkflowParameter
    workflow_mod.WorkflowToolDefinition = _WorkflowToolDefinition
    sys.modules["models"] = models_pkg
    sys.modules["models.workflow"] = workflow_mod

    env = env_override or {}
    with patch.dict("os.environ", env, clear=False):
        path = Path("comfyui-mcp/managers/workflow_manager.py")
        spec = importlib.util.spec_from_file_location(
            f"test_wfm_{id(env)}", path
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def test_env_var_overrides_default_model():
    module = _load_module({"COMFY_MCP_DEFAULT_MODEL": "flux1-schnell-fp8.safetensors"})
    assert module.OPTIONAL_PARAM_DEFAULTS["model"] == "flux1-schnell-fp8.safetensors"


def test_no_env_var_uses_fallback():
    """Without env var, should use a sensible fallback (not crash)."""
    env = {k: v for k, v in __import__("os").environ.items() if k != "COMFY_MCP_DEFAULT_MODEL"}
    module = _load_module()
    # Should have some string value, not None
    assert isinstance(module.OPTIONAL_PARAM_DEFAULTS["model"], str)
    assert len(module.OPTIONAL_PARAM_DEFAULTS["model"]) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_default_model_env.py -v`
Expected: `test_env_var_overrides_default_model` FAILS because current code ignores the env var.

- [ ] **Step 3: Update workflow_manager.py to read env var**

In `comfyui-mcp/managers/workflow_manager.py`, replace line 55:

```python
    "model": "v1-5-pruned-emaonly.ckpt",
```

with:

```python
    "model": os.environ.get("COMFY_MCP_DEFAULT_MODEL", "v1-5-pruned-emaonly.ckpt"),
```

Also update line 35 description to reflect this:

```python
    "model": "Checkpoint model name. Set COMFY_MCP_DEFAULT_MODEL env var to override. Default: 'v1-5-pruned-emaonly.ckpt'.",
```

- [ ] **Step 4: Run tests**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_default_model_env.py -v`
Expected: All PASS.

- [ ] **Step 5: Apply same fix to dashboard/param_placeholders.py**

In `dashboard/param_placeholders.py`, replace line 43:

```python
    "model": "v1-5-pruned-emaonly.ckpt",
```

with:

```python
    "model": os.environ.get("COMFY_MCP_DEFAULT_MODEL", "v1-5-pruned-emaonly.ckpt"),
```

Add `import os` at the top if not already present.

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_workflow_manager_defaults.py tests/test_orchestration_api.py tests/test_comfyui_default_model_env.py -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add comfyui-mcp/managers/workflow_manager.py dashboard/param_placeholders.py tests/test_comfyui_default_model_env.py
git commit -m "fix(comfyui-mcp): make default model configurable via COMFY_MCP_DEFAULT_MODEL env var"
```

---

### Task 4: Set `COMFY_MCP_DEFAULT_MODEL` in docker-compose and .env.example

**Files:**
- Modify: `docker-compose.yml` (comfyui-mcp service environment)
- Modify: `.env.example`

- [ ] **Step 1: Add env var to docker-compose.yml comfyui-mcp service**

Find the `comfyui-mcp` service in `docker-compose.yml` and add `COMFY_MCP_DEFAULT_MODEL` to its environment block:

```yaml
      COMFY_MCP_DEFAULT_MODEL: ${COMFY_MCP_DEFAULT_MODEL:-flux1-schnell-fp8.safetensors}
```

This defaults to Flux (which is already pulled) but allows override via `.env`.

- [ ] **Step 2: Add env var to .env.example with documentation**

Add to the ComfyUI section of `.env.example`:

```bash
# Default checkpoint model for ComfyUI MCP generate_image / queue_prompt.
# Must match a filename in data/comfyui-storage/ComfyUI/models/checkpoints/.
# COMFY_MCP_DEFAULT_MODEL=flux1-schnell-fp8.safetensors
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "feat: wire COMFY_MCP_DEFAULT_MODEL into docker-compose with flux1-schnell default"
```

---

### Task 5: Update OpenClaw workspace docs (TOOLS.md, AGENTS.md)

Teach the agent the new workflow: discover nodes/models, compose a graph, submit via `queue_prompt`, poll via `get_comfyui_history`.

**Files:**
- Modify: `openclaw/workspace/TOOLS.md`
- Modify: `openclaw/workspace/TOOLS.md.example`
- Modify: `openclaw/workspace/AGENTS.md.example`

- [ ] **Step 1: Update TOOLS.md — replace the runnable workflows table with the new workflow**

Replace the current ComfyUI section (lines 9-17) in `openclaw/workspace/TOOLS.md` with:

```markdown
**ComfyUI image/audio/video generation:**

Compose workflows from scratch or use saved templates:

| Tool | Purpose |
|------|---------|
| `get_comfyui_models` | List available checkpoints, LoRAs, VAEs |
| `get_comfyui_node_info` | Get node class definitions (inputs, outputs, types) |
| `queue_prompt` | Submit raw API-format workflow JSON directly to ComfyUI |
| `get_comfyui_history` | Poll for results after submission (pass prompt_id) |
| `get_comfyui_queue` | Check if ComfyUI is busy before submitting |
| `free_comfyui_vram` | Free GPU memory before large generations |

**Saved workflow templates** (use with `run_workflow`):

| workflow_id | Type | Key inputs |
|-------------|------|------------|
| `mcp-api/generate_video` | Video (LTX-2.3, 9:16) | `prompt`, `width`=576, `height`=1024, `frames`=121, `fps`=24 |
| `mcp-api/generate_song` | Audio (ACE-Step v1) | `tags` (required), `lyrics` (required) |

**Composing a workflow from scratch:**
1. Call `get_comfyui_models("checkpoints")` to find available models.
2. Call `get_comfyui_node_info("NodeClass")` for each node you need (e.g. `CheckpointLoaderSimple`, `CLIPTextEncode`, `KSampler`, `EmptyLatentImage`, `VAEDecode`, `SaveImage`).
3. Build API-format JSON: keys are node ID strings ("1", "2", ...), each value has `class_type` and `inputs`. Wire outputs as `["node_id", output_index]`.
4. Submit via `queue_prompt` with the JSON string.
5. Poll `get_comfyui_history(prompt_id)` until outputs appear.
```

- [ ] **Step 2: Apply identical changes to TOOLS.md.example**

Copy the same ComfyUI section to `openclaw/workspace/TOOLS.md.example`.

- [ ] **Step 3: Update AGENTS.md.example — add workflow composition guidance**

Add after the "Stack Control" section in `openclaw/workspace/AGENTS.md.example`:

```markdown
## ComfyUI Workflow Composition

When asked to generate images, videos, or audio:

1. **Discover first.** Call `get_comfyui_models("checkpoints")` and `get_comfyui_node_info` for the nodes you plan to use. Do not assume model filenames or node input names — they vary by installation.
2. **Compose the graph.** Build API-format JSON with string node IDs, `class_type`, and `inputs`. Wire node outputs as `["node_id", output_index]`.
3. **Submit directly.** Use `queue_prompt` for one-off generations. Use `save_workflow` + `run_workflow` only when you want a reusable template.
4. **Poll for results.** After `queue_prompt` returns a `prompt_id`, call `get_comfyui_history(prompt_id)` to get output file paths.
5. **Handle errors.** If ComfyUI returns a node error, use `get_comfyui_node_info("NodeClass")` to check the correct input spec and fix the graph.

Do not rely on hardcoded model names. Always discover available models before composing a workflow.
```

- [ ] **Step 4: Commit**

```bash
git add openclaw/workspace/TOOLS.md openclaw/workspace/TOOLS.md.example openclaw/workspace/AGENTS.md.example
git commit -m "docs(openclaw): teach agent to compose and queue_prompt workflows from scratch"
```

---

### Task 6: Remove stale `mcp-api/generate_image` reference

The `mcp-api/generate_image` workflow file never existed. Remove references to it so the agent doesn't try to use a phantom template.

**Files:**
- Modify: `scripts/comfyui/validate_comfyui_pipeline.py:8` (update example in docstring)
- Modify: `scripts/comfyui/models.json:112` (update description referencing generate_image)

- [ ] **Step 1: Update validate_comfyui_pipeline.py example**

In `scripts/comfyui/validate_comfyui_pipeline.py`, line 8, change:

```python
  python scripts/comfyui/validate_comfyui_pipeline.py --base-path C:/dev/ordo-ai-stack --workflow mcp-api/generate_image --model v1-5-pruned-emaonly.ckpt
```

to:

```python
  python scripts/comfyui/validate_comfyui_pipeline.py --base-path C:/dev/ordo-ai-stack --workflow mcp-api/generate_video --model ltx-2.3-22b-dev-fp8.safetensors
```

- [ ] **Step 2: Update models.json sd15 description**

In `scripts/comfyui/models.json`, line 112, change:

```json
      "description": "SD 1.5 v1-5-pruned-emaonly — default for ComfyUI MCP generate_image (4 GB)",
```

to:

```json
      "description": "SD 1.5 v1-5-pruned-emaonly — lightweight checkpoint for basic image generation (4 GB)",
```

- [ ] **Step 3: Commit**

```bash
git add scripts/comfyui/validate_comfyui_pipeline.py scripts/comfyui/models.json
git commit -m "fix: remove stale mcp-api/generate_image references from scripts"
```

---

### Task 7: Integration smoke test

**Files:**
- Create: `tests/test_queue_prompt_integration.py`

- [ ] **Step 1: Write integration test that validates the full tool registration path**

```python
"""Integration: queue_prompt is registered and callable through the MCP tool interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock


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
```

- [ ] **Step 2: Run all tests**

Run: `cd c:/dev/AI-toolkit && python -m pytest tests/test_comfyui_queue_prompt.py tests/test_comfyui_default_model_env.py tests/test_queue_prompt_integration.py -v`
Expected: All PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_queue_prompt_integration.py
git commit -m "test: add queue_prompt integration smoke test"
```
