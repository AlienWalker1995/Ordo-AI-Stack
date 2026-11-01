"""Ordo Last-Mile Video — helper nodes for the one-shot video finishing workflow.

Three small nodes that fill gaps in the installed pack set:
- OrdoWordTimingsToText: ORDO_WORD_TIMINGS -> STRING (feed Ordo transcription into VibeVoice)
- OrdoDVDLogoOverlay: batched per-frame logo overlay with DVD-screensaver bounce
- OrdoAudioConcat: concatenate two ComfyUI AUDIO dicts (main VO + endcard audio)
"""

from .nodes.word_timings_to_text import OrdoWordTimingsToText
from .nodes.dvd_logo_overlay import OrdoDVDLogoOverlay
from .nodes.audio_concat import OrdoAudioConcat
from .nodes.bypass_toggle import OrdoBypassToggle
from .nodes.caption_fixups import OrdoCaptionFixups
from .nodes.llm_refine import OrdoLLMRefine
from .nodes.stage_bypass_panel import OrdoStageBypassPanel

NODE_CLASS_MAPPINGS = {
    "OrdoWordTimingsToText": OrdoWordTimingsToText,
    "OrdoDVDLogoOverlay": OrdoDVDLogoOverlay,
    "OrdoAudioConcat": OrdoAudioConcat,
    "OrdoBypassToggle": OrdoBypassToggle,
    "OrdoCaptionFixups": OrdoCaptionFixups,
    "OrdoLLMRefine": OrdoLLMRefine,
    "OrdoStageBypassPanel": OrdoStageBypassPanel,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OrdoWordTimingsToText": "Ordo Word Timings → Text",
    "OrdoDVDLogoOverlay": "Ordo DVD Logo Overlay",
    "OrdoAudioConcat": "Ordo Audio Concat",
    "OrdoBypassToggle": "Ordo VibeVoice Bypass Toggle",
    "OrdoCaptionFixups": "Ordo Caption Fixups",
    "OrdoLLMRefine": "Ordo LLM Caption Refine",
    "OrdoStageBypassPanel": "Ordo Stage Bypass",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
