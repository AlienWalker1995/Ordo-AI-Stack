"""Default model resolution: env var > auto-detect > hardcoded fallback."""

from __future__ import annotations

import importlib
import importlib.util
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
    module = _load_module()
    # Should have some string value, not None
    assert isinstance(module.OPTIONAL_PARAM_DEFAULTS["model"], str)
    assert len(module.OPTIONAL_PARAM_DEFAULTS["model"]) > 0
