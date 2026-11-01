"""Ordo Caption Style — visual configuration node for captions."""

import logging
from .caption_types import DEFAULT_STYLE, STYLE_PRESETS, PRESET_NAMES, _resolve_preset_font, get_system_fonts

logger = logging.getLogger("Ordo")


class OrdoCaptionStyleNode:
    @classmethod
    def INPUT_TYPES(cls):
        font_list = get_system_fonts()
        default_font = DEFAULT_STYLE["font_path"]
        if default_font not in font_list:
            default_font = font_list[0]
        # Allow "" so older workflows validate; resolved in build_style for Custom preset.
        font_combo = tuple([""] + list(font_list))

        return {
            "required": {
                "preset": (PRESET_NAMES, {"default": "Custom", "tooltip": "Viral caption style preset (overrides all settings below when not Custom)"}),
                "font": (font_combo, {"default": default_font, "tooltip": "TrueType/OpenType font file; leave empty to use preset font or first available"}),
                "font_size": ("INT", {"default": 120, "min": 20, "max": 400, "step": 4, "tooltip": "Font size in pixels"}),
                "fill_color": ("STRING", {"default": "#FFFFFF", "tooltip": "Default word color (hex)"}),
                "highlight_color_1": ("STRING", {"default": "#00FFFF", "tooltip": "Active-word highlight color 1 (hex)"}),
                "highlight_color_2": ("STRING", {"default": "#00FF00", "tooltip": "Active-word highlight color 2 (hex)"}),
                "highlight_color_3": ("STRING", {"default": "#FFFF00", "tooltip": "Active-word highlight color 3 (hex)"}),
                "highlight_color_4": ("STRING", {"default": "#FF4444", "tooltip": "Active-word highlight color 4 (hex)"}),
                "stroke_color": ("STRING", {"default": "#000000", "tooltip": "Text outline color (hex)"}),
                "stroke_width": ("INT", {"default": 8, "min": 0, "max": 30, "step": 1, "tooltip": "Outline thickness in pixels"}),
                "shadow_color": ("STRING", {"default": "#000000", "tooltip": "Drop shadow color (hex)"}),
                "shadow_opacity": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "Shadow opacity"}),
                "shadow_blur": ("FLOAT", {"default": 6.0, "min": 0.0, "max": 30.0, "step": 0.5, "tooltip": "Shadow blur radius"}),
                "anim_duration": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Pop-in animation duration (seconds)"}),
                "y_position": ("FLOAT", {"default": 0.45, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "Vertical position (0=top, 1=bottom)"}),
                "uppercase": ("BOOLEAN", {"default": True, "tooltip": "Convert words to UPPERCASE"}),
            }
        }

    RETURN_TYPES = ("ORDO_CAPTION_STYLE",)
    RETURN_NAMES = ("style",)
    FUNCTION = "build_style"
    CATEGORY = "Ordo"
    DESCRIPTION = "Configure visual style for Ordo caption rendering"

    def build_style(self, preset, font, font_size, fill_color,
                    highlight_color_1, highlight_color_2,
                    highlight_color_3, highlight_color_4,
                    stroke_color, stroke_width, shadow_color,
                    shadow_opacity, shadow_blur, anim_duration,
                    y_position, uppercase):
        if preset != "Custom" and preset in STYLE_PRESETS:
            style = _resolve_preset_font(STYLE_PRESETS[preset])
            style["y_position"] = y_position
            style["font_size"] = font_size
            logger.info(f"Using caption style preset: {preset}")
            return (style,)

        font_list = get_system_fonts()
        effective = (font or "").strip()
        if not effective:
            effective = DEFAULT_STYLE["font_path"] or (font_list[0] if font_list else "")

        style = {
            "font_path": effective,
            "font_size": font_size,
            "fill_color": fill_color,
            "highlight_colors": [highlight_color_1, highlight_color_2,
                                 highlight_color_3, highlight_color_4],
            "stroke_color": stroke_color,
            "stroke_width": stroke_width,
            "shadow_color": shadow_color,
            "shadow_opacity": shadow_opacity,
            "shadow_offset_x": 4,
            "shadow_offset_y": 4,
            "shadow_blur": shadow_blur,
            "anim_duration": anim_duration,
            "y_position": y_position,
            "uppercase": uppercase,
        }
        return (style,)
