"""Convert shot-by-shot breakdown XML (or similar) into one LTX-friendly text prompt."""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger("Ordo")

# Fields we pull into the narrative (order matters for readability).
_PRIORITY_FIELDS = (
    "PHASE",
    "BEAT",
    "SIGNAL",
    "CAM",
    "SUBJ",
    "MOT",
    "ENV",
    "AUDIO",
    "FX",
    "COMP",
    "RENDER",
    "SCENE_LOC",
    "FIRST_IMAGE",
    "LAST_IMAGE",
    "UI",
    "TEXT",
    "UI_UX",
)


def _strip_xml_wrappers(text: str) -> str:
    t = text.strip()
    t = re.sub(r"<\?xml[^>]*\?>", "", t, flags=re.I)
    t = re.sub(r"<!\[CDATA\[", "", t)
    t = re.sub(r"\]\]>", "", t)
    t = re.sub(r"</?shot_by_shot_breakdown[^>]*>", "", t, flags=re.I)
    return t.strip()


def _split_shots(inner: str) -> list[str]:
    """Split on || between shots; tolerate extra whitespace."""
    parts = re.split(r"\s*\|\|\s*", inner.strip())
    return [p.strip() for p in parts if p.strip()]


def _parse_shot_header(part: str) -> Tuple[Optional[str], Optional[str], str]:
    """Return (shot_id, time_range, rest) or (None, None, part)."""
    m = re.match(
        r"(S\d+)\s+\[([^\]]+)\]\s*:\s*(.*)",
        part.strip(),
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), m.group(2).strip(), m.group(3).strip()
    return None, None, part


def _parse_fields(rest: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # Split on ` | ` — field keys are uppercase tokens before first `:`
    for chunk in re.split(r"\s*\|\s*", rest):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        key, _, val = chunk.partition(":")
        key = key.strip().upper()
        val = val.strip()
        if key and val:
            out[key] = val
    return out


def _fields_to_paragraph(
    shot_id: Optional[str],
    time_range: Optional[str],
    fields: dict,
) -> str:
    lines = []
    label = []
    if shot_id:
        label.append(shot_id)
    if time_range:
        label.append(f"[{time_range}]")
    head = " ".join(label) if label else "Scene"
    lines.append(f"{head}.")

    for k in _PRIORITY_FIELDS:
        if k in fields and fields[k] and fields[k].lower() not in ("none.", "none"):
            lines.append(f"{k.replace('_', ' ')}: {fields[k]}")

    # Any remaining keys not in priority
    seen = set(_PRIORITY_FIELDS)
    for k, v in sorted(fields.items()):
        if k in seen:
            continue
        if v and v.lower() not in ("none.", "none"):
            lines.append(f"{k.replace('_', ' ')}: {v}")

    return " ".join(lines)


def convert_shot_breakdown_to_ltx_prompt(raw: str, max_chars: int = 6000) -> str:
    """
    Turn shot-by-shot breakdown markup into one continuous prompt for LTX T2V.

    If no S01-style shots are found, returns stripped text (passthrough).
    """
    if not raw or not raw.strip():
        return ""

    inner = _strip_xml_wrappers(raw)
    shots = _split_shots(inner)

    parsed_blocks = []
    for part in shots:
        sid, tr, rest = _parse_shot_header(part)
        if sid is None:
            # Single blob without standard header — treat whole inner as one block
            continue
        parsed_blocks.append((sid, tr, _parse_fields(rest)))

    if not parsed_blocks:
        # Passthrough: maybe user pasted prose only
        cleaned = " ".join(inner.split())
        if len(cleaned) > max_chars:
            logger.warning("Shot breakdown passthrough truncated to %s chars", max_chars)
            return cleaned[:max_chars]
        return cleaned

    intro = (
        "Single continuous vertical video. Play the following beats in order, "
        "with smooth motion and consistent look. Include camera, environment, and "
        "full audio/sound design as described.\n\n"
    )

    paras = []
    for sid, tr, fields in parsed_blocks:
        paras.append(_fields_to_paragraph(sid, tr, fields))

    body = "\n\n".join(paras)
    out = intro + body
    if len(out) > max_chars:
        logger.warning("LTX prompt truncated from %s to %s chars", len(out), max_chars)
        out = out[:max_chars]
    return out


def _nano_still_plus_interior_context(
    still: str, interior_beats: str, max_chars: int = 450
) -> str:
    """Append a short excerpt of middle-shot 'core' beats so Nano Banana matches LTX interior."""
    still = (still or "").strip()
    interior = (interior_beats or "").strip()
    if not still or not interior:
        return still
    if len(interior) > max_chars:
        interior = interior[:max_chars].rstrip() + "…"
    return (
        f"{still}\n\n"
        f"[Interior beats — match palette/composition with LTX middle: {interior}]"
    )


def _still_prompt_for_shot(
    shot_id: Optional[str],
    time_range: Optional[str],
    fields: dict,
    prefer_key: str,
) -> str:
    """Text for Nano Banana still: prefer FIRST_IMAGE / LAST_IMAGE, else narrative fields."""
    v = (fields.get(prefer_key) or "").strip()
    if v and v.lower() not in ("none.", "none"):
        return v
    trimmed = {
        k: val
        for k, val in fields.items()
        if k not in ("FIRST_IMAGE", "LAST_IMAGE")
    }
    return _fields_to_paragraph(shot_id, time_range, trimmed)


def convert_shot_breakdown_to_ltx_prompt_v3(
    raw: str, max_chars: int = 6000
) -> Tuple[str, str, str]:
    """
    Middle-only LTX prompt (drops first and last shot blocks from the narrative),
    plus prompts for first/last stills (Nano Banana 2).

    Returns (ltx_prompt_middle, first_frame_prompt, last_frame_prompt).
    """
    if not raw or not raw.strip():
        return ("", "", "")

    inner = _strip_xml_wrappers(raw)
    shots = _split_shots(inner)

    parsed_blocks: list[tuple[Optional[str], Optional[str], dict[str, str]]] = []
    for part in shots:
        sid, tr, rest = _parse_shot_header(part)
        if sid is None:
            continue
        parsed_blocks.append((sid, tr, _parse_fields(rest)))

    if not parsed_blocks:
        cleaned = " ".join(inner.split())
        if len(cleaned) > max_chars:
            logger.warning("Shot breakdown v3 passthrough truncated to %s chars", max_chars)
            cleaned = cleaned[:max_chars]
        return (cleaned, "", "")

    first_sid, first_tr, first_fields = parsed_blocks[0]
    last_sid, last_tr, last_fields = parsed_blocks[-1]

    first_still = _still_prompt_for_shot(
        first_sid, first_tr, first_fields, "FIRST_IMAGE"
    )
    last_still = _still_prompt_for_shot(
        last_sid, last_tr, last_fields, "LAST_IMAGE"
    )

    if len(parsed_blocks) == 1:
        middle_shots: list[tuple[Optional[str], Optional[str], dict]] = []
    else:
        middle_shots = parsed_blocks[1:-1]

    intro = (
        "Single continuous vertical video. Play the following beats in order, "
        "with smooth motion and consistent look. Include camera, environment, and "
        "full audio/sound design as described. First and last frames are fixed by "
        "reference stills; animate the middle.\n\n"
    )

    if not middle_shots:
        body = ""
    else:
        paras = [
            _fields_to_paragraph(sid, tr, flds) for sid, tr, flds in middle_shots
        ]
        body = "\n\n".join(paras)

    # Nano Banana prompts: still line from FIRST_IMAGE / LAST_IMAGE, plus same
    # interior beat text that becomes LTX `ltx_prompt_middle` (style continuity).
    interior_for_nano = body.strip() if body else ""
    first_still = _nano_still_plus_interior_context(first_still, interior_for_nano)
    last_still = _nano_still_plus_interior_context(last_still, interior_for_nano)

    out_middle = intro + body if body else intro.rstrip()
    if len(out_middle) > max_chars:
        logger.warning(
            "LTX v3 middle prompt truncated from %s to %s chars",
            len(out_middle),
            max_chars,
        )
        out_middle = out_middle[:max_chars]

    return (out_middle, first_still, last_still)


class OrdoShotBreakdownToLTXPromptNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shot_data": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": "Paste <shot_by_shot_breakdown> XML, or shot lines with S01 [time]: ... | BEAT: ... || S02 ...",
                    },
                ),
                "max_output_chars": (
                    "INT",
                    {
                        "default": 6000,
                        "min": 512,
                        "max": 32000,
                        "step": 256,
                        "tooltip": "Hard cap on prompt length for the text encoder",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("ltx_prompt",)
    FUNCTION = "convert"
    CATEGORY = "Ordo"
    DESCRIPTION = (
        "Parses shot-by-shot breakdown XML into one LTX-ready prompt string"
    )

    def convert(self, shot_data: str, max_output_chars: int = 6000):
        text = convert_shot_breakdown_to_ltx_prompt(shot_data, max_chars=max_output_chars)
        n = len([m for m in re.finditer(r"\bS\d+\s*\[", shot_data or "", re.I)])
        logger.info(
            "OrdoShotBreakdownToLTXPrompt: output %s chars (~%s shot headers in source)",
            len(text),
            n,
        )
        return (text,)


class OrdoShotBreakdownToLTXPromptV3Node:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shot_data": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "",
                        "tooltip": (
                            "Shot breakdown XML or S01 [t]: ... || S02 ... "
                            "FIRST_IMAGE on the first shot and LAST_IMAGE on the final shot "
                            "feed Nano Banana (with a short excerpt of middle beats for continuity). "
                            "LTX CLIP uses middle shots only."
                        ),
                    },
                ),
                "max_output_chars": (
                    "INT",
                    {
                        "default": 6000,
                        "min": 512,
                        "max": 32000,
                        "step": 256,
                        "tooltip": "Hard cap on middle LTX prompt length",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("ltx_prompt_middle", "first_frame_prompt", "last_frame_prompt")
    FUNCTION = "convert"
    CATEGORY = "Ordo"
    DESCRIPTION = (
        "Shot breakdown → `ltx_prompt_middle` (interior shots only for LTX CLIP) plus "
        "`first_frame_prompt` / `last_frame_prompt` for Nano Banana 2: taken from the "
        "first shot's FIRST_IMAGE and the last shot's LAST_IMAGE (with a short excerpt of "
        "the same middle beats appended for style continuity)."
    )

    def convert(self, shot_data: str, max_output_chars: int = 6000):
        mid, first_p, last_p = convert_shot_breakdown_to_ltx_prompt_v3(
            shot_data, max_chars=max_output_chars
        )
        n = len([m for m in re.finditer(r"\bS\d+\s*\[", shot_data or "", re.I)])
        logger.info(
            "OrdoShotBreakdownToLTXPromptV3: middle %s chars; still prompts %s / %s chars (~%s shot headers)",
            len(mid),
            len(first_p),
            len(last_p),
            n,
        )
        return (mid, first_p, last_p)
