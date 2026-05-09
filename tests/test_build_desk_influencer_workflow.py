import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_desk_influencer_workflow.py"
SOURCE = REPO_ROOT / "data/comfyui-storage/ComfyUI/user/default/workflows/ltx-video/LTX-2.3_-_Street_Interview.json"
TARGET = REPO_ROOT / "data/comfyui-storage/ComfyUI/user/default/workflows/ltx-video/LTX-2.3_-_Desk_Influencer.json"

# These tests require an operator-local ComfyUI workflow fixture under
# `data/comfyui-storage/...`, which is gitignored. CI runners don't have it.
# Skip cleanly there; run normally on dev boxes that do have the fixture.
pytestmark = pytest.mark.skipif(
    not SOURCE.exists(),
    reason=f"workflow fixture not present: {SOURCE.relative_to(REPO_ROOT)}",
)


def run_builder(tmp_target: Path) -> dict:
    """Run the builder pointing at a tmp output path; return parsed JSON."""
    subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(SOURCE), "--target", str(tmp_target)],
        check=True,
    )
    return json.loads(tmp_target.read_text(encoding="utf-8"))


def test_builder_produces_valid_json(tmp_path):
    out = run_builder(tmp_path / "out.json")
    assert set(out.keys()) >= {"nodes", "links", "groups", "version"}
    assert isinstance(out["nodes"], list) and len(out["nodes"]) > 0


def test_cameraman_lora_strength_is_0_15(tmp_path):
    out = run_builder(tmp_path / "out.json")
    power_lora = next(n for n in out["nodes"] if n.get("id") == 301)
    cameraman = next(
        w for w in power_lora["widgets_values"]
        if isinstance(w, dict) and w.get("lora", "").startswith("LTX-2.3-Cameraman")
    )
    assert cameraman["on"] is True
    assert cameraman["strength"] == 0.15


def test_negative_prompt_has_desk_additions_and_drops_microphone(tmp_path):
    out = run_builder(tmp_path / "out.json")
    neg_node = next(n for n in out["nodes"] if n.get("id") == 110)
    text = neg_node["widgets_values"][0]
    for phrase in ["handheld camera", "shaky cam", "two desks", "background morphing", "lighting changing mid-shot"]:
        assert phrase in text, f"expected {phrase!r} in negative prompt"
    assert "identity" in text.lower() or "morph" in text.lower()
    assert "missing microphone" not in text


def test_shot_data_is_desk_template(tmp_path):
    out = run_builder(tmp_path / "out.json")
    shot_node = next(n for n in out["nodes"] if n.get("id") == 352)
    text = shot_node["widgets_values"][0]
    assert "podcast desk" in text
    assert "ring light" in text
    assert "NYC sidewalk" not in text
    assert "vox-pop" not in text.lower()
    assert "[VISUAL]" in text and "[SPEECH]" in text and "[SOUNDS]" in text


def test_all_id_loras_off_at_strength_one(tmp_path):
    out = run_builder(tmp_path / "out.json")
    power_lora = next(n for n in out["nodes"] if n.get("id") == 301)
    id_loras = [
        w for w in power_lora["widgets_values"]
        if isinstance(w, dict) and "ID-LoRA" in w.get("lora", "")
    ]
    assert id_loras, "expected at least one ID-LoRA row in Power Lora Loader"
    # Spec mode-switch (anchored default): ALL ID-LoRA rows must be OFF
    # at strength 1.0 so operator can flip exactly one ON for re-angle mode.
    assert all(w["on"] is False for w in id_loras), \
        f"expected every ID-LoRA row OFF, got {[(w['lora'], w['on']) for w in id_loras]}"
    assert all(w["strength"] == 1 for w in id_loras)


def test_builder_is_idempotent(tmp_path):
    """Running the builder on its own output should produce identical JSON.

    All mutations set to fixed values rather than applying deltas, so
    re-running must be a no-op. Locks that property in.
    """
    first = tmp_path / "first.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(SOURCE), "--target", str(first)],
        check=True,
    )
    # Now re-build using the previous output as source
    second = tmp_path / "second.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(first), "--target", str(second)],
        check=True,
    )
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
