"""Build LTX-2.3 Desk Influencer workflow from the street-interview clone.

Applies parameter-level mutations only. Structural I2V additions are
performed by the operator in ComfyUI's UI per the accompanying README.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


DESK_NEGATIVE_PROMPT = (
    "subject changing identity mid-shot, face morphing, identity drift between frames, "
    "multiple subjects, two people in frame, crowd, "
    "handheld camera, shaky cam, dutch angle, fisheye, vlog selfie distortion, "
    "two desks, multiple desks, room layout changing, walls warping, "
    "background morphing, lighting changing mid-shot, props moving on desk"
)


def _replace_negative_prompt(data: dict) -> None:
    neg_node = next(n for n in data["nodes"] if n.get("id") == 110)
    neg_node["widgets_values"][0] = DESK_NEGATIVE_PROMPT


DESK_SHOT_DATA = """[VISUAL]: man in his early 30s seated at a modern podcast desk, soft ring light from upper-left, a single condenser mic on a low boom in front of him, monitor glow behind, clean tech-podcast aesthetic. He's looking directly at the camera, talking energetically with hand gestures. Vertical 9:16 framing, chest-up, depth of field on the background.

[SPEECH]: \"...\"

[SOUNDS]: room tone, soft keyboard tap, distant HVAC."""


def _replace_shot_data(data: dict) -> None:
    shot_node = next(n for n in data["nodes"] if n.get("id") == 352)
    shot_node["widgets_values"][0] = DESK_SHOT_DATA


def _retune_cameraman_lora(data: dict) -> None:
    """Drop Cameraman IC-LoRA strength from street-interview's 0.5 to 0.15.

    Desk content is mostly static. 0.15 keeps a touch of breath without
    pushing toward handheld. Operator can crank to 0.3 for vlog energy.
    """
    power_lora = next(n for n in data["nodes"] if n.get("id") == 301)
    for widget in power_lora["widgets_values"]:
        if isinstance(widget, dict) and widget.get("lora", "").startswith("LTX-2.3-Cameraman"):
            widget["strength"] = 0.15
            return
    raise RuntimeError("Cameraman IC-LoRA row not found in Power Lora Loader (node 301)")


def build(source: Path, target: Path) -> None:
    data = json.loads(source.read_text(encoding="utf-8"))
    _retune_cameraman_lora(data)
    _replace_negative_prompt(data)
    _replace_shot_data(data)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    args = p.parse_args()
    build(args.source, args.target)
    print(f"wrote {args.target}")


if __name__ == "__main__":
    main()
