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


def _node_by_id(data: dict, node_id: int, expected_type_substring: str) -> dict:
    """Find a node by id and assert its type contains the expected substring.

    Bare next() lookups raise StopIteration with no context if a node is
    removed, and silently overwrite the wrong widget if an ID gets reused
    for a different node type. This wrapper makes both failure modes loud.
    """
    for node in data["nodes"]:
        if node.get("id") == node_id:
            actual_type = node.get("type", "")
            if expected_type_substring not in actual_type:
                raise RuntimeError(
                    f"Node {node_id} expected to contain {expected_type_substring!r} in its type, "
                    f"got {actual_type!r}"
                )
            return node
    raise RuntimeError(f"Node id {node_id} not found in workflow")


def _replace_negative_prompt(data: dict) -> None:
    """Swap negative prompt for desk-specific rejections.

    Drops street-interview's "missing microphone" clause (desk mics
    don't need to be on-frame for influencer content) and adds rejections
    for handheld/shaky/dutch-angle camera, multi-desk scenes, and
    background morphing. Identity-drift rejections are preserved.
    """
    neg_node = _node_by_id(data, 110, "CLIPTextEncode")
    neg_node["widgets_values"][0] = DESK_NEGATIVE_PROMPT


DESK_SHOT_DATA = """[VISUAL]: man in his early 30s seated at a modern podcast desk, soft ring light from upper-left, a single condenser mic on a low boom in front of him, monitor glow behind, clean tech-podcast aesthetic. He's looking directly at the camera, talking energetically with hand gestures. Vertical 9:16 framing, chest-up, depth of field on the background.

[SPEECH]: \"...\"

[SOUNDS]: room tone, soft keyboard tap, distant HVAC."""


def _replace_shot_data(data: dict) -> None:
    """Replace the SHOT_DATA primitive with the desk-influencer template.

    Swaps street-interview's NYC-sidewalk vox-pop scene for a podcast-desk
    aesthetic (ring light, condenser mic on boom, monitor glow) while
    preserving the [VISUAL]/[SPEECH]/[SOUNDS] structured-prompt format
    that the downstream encoder expects.
    """
    shot_node = _node_by_id(data, 352, "Primitive")
    shot_node["widgets_values"][0] = DESK_SHOT_DATA


def _normalize_id_loras(data: dict) -> None:
    """Force every ID-LoRA row in the Power Lora Loader to {on: False, strength: 1.0}.

    Anchored mode (the desk-influencer default per the spec's mode-switch
    table) wants no identity injection — the master scene still + prompt
    carry the character. Operator flips a single ID-LoRA row ON in
    ComfyUI's UI for re-angle mode.

    The street-interview source has multiple ID-LoRA rows (TalkVid x2,
    CelebVHQ x1), some on, some off. We iterate the whole list — early
    return would leave duplicate-on rows live.
    """
    power_lora = _node_by_id(data, 301, "Power Lora Loader")
    found = 0
    for widget in power_lora["widgets_values"]:
        if isinstance(widget, dict) and "ID-LoRA" in widget.get("lora", ""):
            widget["on"] = False
            widget["strength"] = 1
            found += 1
    if found == 0:
        raise RuntimeError("No ID-LoRA rows found in Power Lora Loader (node 301)")


def _retune_cameraman_lora(data: dict) -> None:
    """Drop Cameraman IC-LoRA strength from street-interview's 0.5 to 0.15.

    Desk content is mostly static. 0.15 keeps a touch of breath without
    pushing toward handheld. Operator can crank to 0.3 for vlog energy.
    """
    power_lora = _node_by_id(data, 301, "Power Lora Loader")
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
    _normalize_id_loras(data)
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
