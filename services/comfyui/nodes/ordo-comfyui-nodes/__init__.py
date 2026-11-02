"""
Ordo-ComfyUI — Animated captions + AI image generation node pack.

Registers:
  - 6 caption nodes under Ordo/ category
  - 1 Load Text utility node
  - 3 AI Image Generation nodes under Ordo/AI category
  - 2 Shot-breakdown → LTX prompt nodes
"""

__version__ = "1.0.0"
__author__ = "Ordo"
__title__ = "Ordo ComfyUI"

import logging

# ── Logging ──────────────────────────────────────────────────────────────

logger = logging.getLogger("Ordo")
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[Ordo] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ── Node registration ────────────────────────────────────────────────────

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# ── Caption nodes (always available) ─────────────────────────────────────

try:
    from .nodes.caption_studio_node import OrdoCaptionStudioNode
    from .nodes.caption_style_node import OrdoCaptionStyleNode
    from .nodes.render_captions_node import OrdoRenderCaptionsNode
    from .nodes.timing_editor_node import OrdoTimingEditorNode
    from .nodes.transcribe_node import OrdoTranscribeAudioNode
    from .nodes.tts_node import OrdoTTSEdgeNode

    NODE_CLASS_MAPPINGS["OrdoCaptionStyle"] = OrdoCaptionStyleNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoCaptionStyle"] = "Ordo Caption Style"

    NODE_CLASS_MAPPINGS["OrdoTranscribeAudio"] = OrdoTranscribeAudioNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoTranscribeAudio"] = "Ordo Transcribe Audio"

    NODE_CLASS_MAPPINGS["OrdoTTSEdge"] = OrdoTTSEdgeNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoTTSEdge"] = "Ordo TTS (Edge)"

    NODE_CLASS_MAPPINGS["OrdoRenderCaptions"] = OrdoRenderCaptionsNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoRenderCaptions"] = "Ordo Render Captions"

    NODE_CLASS_MAPPINGS["OrdoTimingEditor"] = OrdoTimingEditorNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoTimingEditor"] = "Ordo Timing Editor"

    NODE_CLASS_MAPPINGS["OrdoCaptionStudio"] = OrdoCaptionStudioNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoCaptionStudio"] = "Ordo Caption Studio"

    logger.info("Caption nodes registered (6 nodes)")

except Exception as e:
    logger.error(f"Failed to register caption nodes: {e}")

# ── Load Text node (always available) ────────────────────────────────────

try:
    from .nodes.load_text_node import OrdoLoadTextFromFileNode
    NODE_CLASS_MAPPINGS["OrdoLoadTextFromFile"] = OrdoLoadTextFromFileNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoLoadTextFromFile"] = "Ordo Load Text From File"
except Exception as e:
    logger.error(f"Failed to register LoadTextFromFile node: {e}")

# ── AI Image Generation nodes ─────────────────────────────────────────────

try:
    from .nodes.nano_banana2_edit_node import OrdoNanoBanana2EditNode
    from .nodes.nano_banana2_node import OrdoNanoBanana2Node
    from .nodes.proxy_config_node import OrdoProxyConfigNode
    NODE_CLASS_MAPPINGS["OrdoProxyConfig"] = OrdoProxyConfigNode
    NODE_CLASS_MAPPINGS["OrdoNanoBanana2"] = OrdoNanoBanana2Node
    NODE_CLASS_MAPPINGS["OrdoNanoBanana2Edit"] = OrdoNanoBanana2EditNode
    NODE_DISPLAY_NAME_MAPPINGS["OrdoProxyConfig"] = "Ordo Proxy Config"
    NODE_DISPLAY_NAME_MAPPINGS["OrdoNanoBanana2"] = "Ordo Nano Banana 2"
    NODE_DISPLAY_NAME_MAPPINGS["OrdoNanoBanana2Edit"] = "Ordo Nano Banana 2 Edit"
    logger.info("AI Image Generation nodes registered (3 nodes)")
except Exception as e:
    logger.error(f"Failed to register AI Image Generation nodes: {e}")

# ── Shot-breakdown → LTX prompt nodes ─────────────────────────────────────

try:
    from .nodes.shot_breakdown_to_ltx_prompt_node import (
        OrdoShotBreakdownToLTXPromptNode,
        OrdoShotBreakdownToLTXPromptV3Node,
    )
    NODE_CLASS_MAPPINGS["OrdoShotBreakdownToLTXPrompt"] = OrdoShotBreakdownToLTXPromptNode
    NODE_CLASS_MAPPINGS["OrdoShotBreakdownToLTXPromptV3"] = OrdoShotBreakdownToLTXPromptV3Node
    NODE_DISPLAY_NAME_MAPPINGS["OrdoShotBreakdownToLTXPrompt"] = "Ordo Shot Breakdown → LTX Prompt"
    NODE_DISPLAY_NAME_MAPPINGS["OrdoShotBreakdownToLTXPromptV3"] = "Ordo Shot Breakdown → LTX Prompt v3"
    logger.info("Shot-breakdown → LTX prompt nodes registered (2 nodes)")
except Exception as e:
    logger.error(f"Failed to register shot-breakdown nodes: {e}")

WEB_DIRECTORY = "./web"

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', 'WEB_DIRECTORY', '__version__']
