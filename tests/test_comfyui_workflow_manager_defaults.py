from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType


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


def _load_workflow_manager_module():
    models_pkg = ModuleType("models")
    workflow_mod = ModuleType("models.workflow")
    workflow_mod.WorkflowParameter = _WorkflowParameter
    workflow_mod.WorkflowToolDefinition = _WorkflowToolDefinition
    sys.modules["models"] = models_pkg
    sys.modules["models.workflow"] = workflow_mod

    path = Path("comfyui-mcp/managers/workflow_manager.py")
    spec = importlib.util.spec_from_file_location("test_workflow_manager", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_catalog_infers_audio_defaults(tmp_path: Path):
    module = _load_workflow_manager_module()
    workflow_dir = tmp_path / "workflows" / "mcp-api"
    workflow_dir.mkdir(parents=True)
    workflow_path = workflow_dir / "generate_song.json"
    workflow_path.write_text(
        json.dumps(
            {
                "14": {
                    "class_type": "TextEncodeAceStepAudio",
                    "inputs": {
                        "tags": "PARAM_STR_TAGS",
                        "lyrics": "PARAM_STR_LYRICS",
                        "lyrics_strength": "PARAM_FLOAT_LYRICS_STRENGTH",
                    },
                },
                "17": {
                    "class_type": "EmptyAceStepLatentAudio",
                    "inputs": {"seconds": "PARAM_INT_SECONDS"},
                },
                "52": {
                    "class_type": "KSampler",
                    "inputs": {"seed": "PARAM_INT_SEED"},
                },
            }
        ),
        encoding="utf-8",
    )
    workflow_path.with_suffix(".wfmeta").write_text(
        json.dumps(
            {
                "available_inputs": {
                    "style_prompt": {
                        "type": "str",
                        "required": False,
                        "description": "Alias for tags.",
                    },
                    "tags": {
                        "type": "str",
                        "required": True,
                        "description": "Style prompt.",
                    },
                    "lyrics": {
                        "type": "str",
                        "required": True,
                        "description": "Lyrics.",
                    },
                    "cfg": {
                        "type": "float",
                        "required": False,
                        "description": "CFG.",
                    },
                },
                "override_mappings": {
                    "style_prompt": [["14", "tags"]],
                    "tags": [["14", "tags"]],
                    "lyrics": [["14", "lyrics"]],
                    "cfg": [["52", "seed"]],
                },
                "defaults": {
                    "lyrics_strength": 0.99,
                    "seconds": 60,
                    "cfg": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )

    manager = module.WorkflowManager(workflow_dir.parent)
    catalog = manager.get_workflow_catalog()
    song_entry = next(item for item in catalog if item["id"] == "mcp-api/generate_song")

    assert song_entry["defaults"]["seconds"] == 60
    assert song_entry["defaults"]["lyrics_strength"] == 0.99
    assert song_entry["defaults"]["cfg"] == 1.0
    assert "style_prompt" in song_entry["available_inputs"]
