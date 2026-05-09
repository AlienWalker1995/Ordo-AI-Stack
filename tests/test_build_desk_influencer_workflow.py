import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_desk_influencer_workflow.py"
SOURCE = REPO_ROOT / "data/comfyui-storage/ComfyUI/user/default/workflows/ltx-video/LTX-2.3_-_Street_Interview.json"
TARGET = REPO_ROOT / "data/comfyui-storage/ComfyUI/user/default/workflows/ltx-video/LTX-2.3_-_Desk_Influencer.json"


def run_builder(tmp_target: Path) -> dict:
    """Run the builder pointing at a tmp output path; return parsed JSON."""
    subprocess.run(
        ["python", str(SCRIPT), "--source", str(SOURCE), "--target", str(tmp_target)],
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
