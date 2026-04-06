from __future__ import annotations

import json
from pathlib import Path


def test_generate_song_workflow_is_generic_and_parameterized():
    workflow_path = Path(
        "data/comfyui-storage/ComfyUI/user/default/workflows/mcp-api/generate_song.json"
    )
    metadata_path = workflow_path.with_suffix(".wfmeta")

    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    text_encode = workflow["94"]["inputs"]
    latent = workflow["98"]["inputs"]
    sampler = workflow["3"]["inputs"]

    assert text_encode["text"] == "PARAM_STR_TAGS"
    assert text_encode["lyrics"] == "PARAM_STR_LYRICS"
    assert text_encode["duration"] == "PARAM_INT_SECONDS"
    assert latent["seconds"] == "PARAM_INT_SECONDS"
    assert sampler["seed"] == "PARAM_INT_SEED"

    serialized = json.dumps(workflow).lower()
    assert "pub stuffff" not in serialized
    assert "irish folk song" not in serialized

    assert metadata["available_inputs"]["tags"]["required"] is True
    assert metadata["available_inputs"]["lyrics"]["required"] is True
    assert "style_prompt" in metadata["available_inputs"]
    assert metadata["override_mappings"]["style_prompt"] == [["94", "text"]]
    assert metadata["override_mappings"]["tags"] == [["94", "text"]]
    assert metadata["override_mappings"]["language"] == [["94", "language"]]
    assert metadata["override_mappings"]["key"] == [["94", "key"]]
    assert metadata["override_mappings"]["cfg"] == [["3", "cfg"]]
    assert metadata["override_mappings"]["steps"] == [["3", "steps"]]
    assert metadata["defaults"]["cfg"] == 1.0
    assert metadata["defaults"]["steps"] == 8
    assert metadata["defaults"]["scheduler"] == "simple"
