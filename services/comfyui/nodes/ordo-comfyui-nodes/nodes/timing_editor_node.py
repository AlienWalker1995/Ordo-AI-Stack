"""Ordo Timing Editor — visual timeline for adjusting word-level timings."""

import json
import logging

logger = logging.getLogger("Ordo")


class OrdoTimingEditorNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "word_timings": ("ORDO_WORD_TIMINGS", {
                    "tooltip": "Word-level timestamps from Transcribe or TTS",
                }),
                "fps": ("FLOAT", {
                    "default": 30.0, "min": 1.0, "max": 120.0, "step": 0.1,
                    "tooltip": "Frames per second (for time ruler reference)",
                }),
            },
            "hidden": {
                "edited_timings": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("ORDO_WORD_TIMINGS",)
    RETURN_NAMES = ("word_timings",)
    OUTPUT_NODE = True
    FUNCTION = "edit_timings"
    CATEGORY = "Ordo"
    DESCRIPTION = "Visual timeline editor for adjusting individual word timings"

    def edit_timings(self, word_timings, fps=30.0, edited_timings=""):
        # If user has made edits in the JS widget, use those
        if edited_timings and edited_timings.strip():
            try:
                parsed = json.loads(edited_timings)
                if isinstance(parsed, list) and len(parsed) > 0:
                    # Validate structure
                    required_keys = {"word", "start", "end"}
                    if all(required_keys.issubset(w.keys()) for w in parsed):
                        # Ensure segment_idx and track exist on every entry
                        for w in parsed:
                            if "segment_idx" not in w:
                                w["segment_idx"] = -1
                            if "track" not in w:
                                w["track"] = 0
                        logger.info(
                            f"Timing Editor: using {len(parsed)} edited word timings"
                        )
                        return {
                            "ui": {
                                "timings_json": [json.dumps(word_timings)],
                                "fps": [fps],
                            },
                            "result": (parsed,),
                        }
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"Timing Editor: invalid edited_timings JSON, "
                               f"passing through original — {e}")

        # Pass through original timings unchanged
        logger.info(
            f"Timing Editor: passing through {len(word_timings)} word timings"
        )
        return {
            "ui": {
                "timings_json": [json.dumps(word_timings)],
                "fps": [fps],
            },
            "result": (word_timings,),
        }
