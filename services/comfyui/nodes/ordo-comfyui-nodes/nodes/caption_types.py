"""
Ordo Caption Types — shared data definitions and defaults.

ORDO_WORD_TIMINGS: list of dicts, each with keys:
    - "word": str
    - "start": float (seconds)
    - "end": float (seconds)
    - "segment_idx": int (which caption group this word belongs to, -1 = ungrouped)

ORDO_CAPTION_STYLE: dict with visual configuration keys (see DEFAULT_STYLE).
"""

import logging
import os
import sys

logger = logging.getLogger("Ordo")

OVERRIDE_FIELDS = (
    "track",                  # int, default 0
    "override_text",          # str | None — display text override
    "override_color",         # str | None — hex fill color when inactive
    "override_highlight",     # str | None — hex color when active/highlighted
    "override_scale",         # float | None — size multiplier (0.5–3.0, 1.0=normal)
    "override_font_size",     # int | None — pixel size override
    "override_stroke_width",  # int | None — stroke width override
    "override_stroke_color",  # str | None — hex stroke color override
    "override_hidden",        # bool — skip rendering (default False)
)


# ── Cross-platform font discovery ────────────────────────────────────────

_THIS_DIR = os.path.dirname(__file__)
_BUNDLED_FONTS_DIR = next(
    (d for d in [
        os.path.join(_THIS_DIR, "fonts"),                    # deployed: nodes/ is pack root
        os.path.join(os.path.dirname(_THIS_DIR), "fonts"),   # local dev: fonts/ at repo root
    ] if os.path.isdir(d)),
    os.path.join(_THIS_DIR, "fonts"),
)


def _get_font_dirs():
    """Return a list of font directories: bundled first, then system dirs."""
    platform = sys.platform
    dirs = []
    # Bundled fonts ship with this package — always available
    if os.path.isdir(_BUNDLED_FONTS_DIR):
        dirs.append(_BUNDLED_FONTS_DIR)
    # System fonts
    if platform == "win32":
        windir = os.environ.get("WINDIR", "C:/Windows")
        dirs.append(os.path.join(windir, "Fonts"))
    elif platform == "darwin":
        dirs.extend([
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ])
    else:  # Linux / other Unix
        dirs.extend([
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts"),
        ])
    return [d for d in dirs if os.path.isdir(d)]


def _find_fonts_in_dirs(font_dirs):
    """Recursively find all .ttf/.otf files under the given directories."""
    fonts = []
    for base in font_dirs:
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith((".ttf", ".otf")):
                    fonts.append(os.path.join(root, f))
    return sorted(set(fonts))


def _find_font_by_name(name_lower, font_list):
    """Find the best matching font path from a list, given a lowercase filename stem.

    Tries exact filename match first, then substring match.
    Returns None if not found.
    """
    for path in font_list:
        if os.path.basename(path).lower() == name_lower:
            return path
    for path in font_list:
        if name_lower.replace(".ttf", "").replace(".otf", "") in os.path.basename(path).lower():
            return path
    return None


# Font name → list of candidate filenames (lowercase) in preference order.
# Used by presets to resolve fonts cross-platform.
_FONT_CANDIDATES = {
    "impact":    ["impact.ttf"],
    "arial":     ["arial.ttf", "Arial.ttf"],
    "arial_bold": ["arialbd.ttf", "Arial Bold.ttf", "Arial-Bold.ttf"],
    "segoe_ui":  ["segoeui.ttf", "SegoeUI.ttf"],
    "segoe_ui_bold": ["segoeuib.ttf", "SegoeUIBold.ttf"],
    "georgia":   ["georgia.ttf", "Georgia.ttf"],
}

# Platform-specific fallback chains when a Windows font isn't available
_FONT_FALLBACKS = {
    "impact":    ["Impact.ttf", "Anton-Regular.ttf",
                  "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf",
                  "FreeSansBold.ttf", "Arial Bold.ttf", "Helvetica-Bold.ttf"],
    "arial":     ["Arial.ttf", "Roboto-Regular.ttf",
                  "LiberationSans-Regular.ttf", "DejaVuSans.ttf",
                  "FreeSans.ttf", "Helvetica.ttf"],
    "arial_bold": ["Arial Bold.ttf", "Arial-Bold.ttf", "arialbd.ttf",
                   "Roboto-Bold.ttf",
                   "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf",
                   "FreeSansBold.ttf", "Helvetica-Bold.ttf"],
    "segoe_ui":  ["SegoeUI.ttf", "segoeui.ttf", "Roboto-Regular.ttf",
                  "LiberationSans-Regular.ttf",
                  "DejaVuSans.ttf", "Helvetica.ttf", "Arial.ttf"],
    "segoe_ui_bold": ["SegoeUIBold.ttf", "segoeuib.ttf", "Roboto-Bold.ttf",
                      "LiberationSans-Bold.ttf",
                      "DejaVuSans-Bold.ttf", "Helvetica-Bold.ttf"],
    "georgia":   ["Georgia.ttf", "georgia.ttf", "PlayfairDisplay-Variable.ttf",
                  "LiberationSerif-Regular.ttf",
                  "DejaVuSerif.ttf", "FreeSerif.ttf", "Times New Roman.ttf"],
}


def _resolve_font(font_key, all_fonts):
    """Resolve a logical font key to an actual font path on this system.

    Args:
        font_key: One of the keys in _FONT_CANDIDATES (e.g. "impact").
        all_fonts: List of all discovered font paths.

    Returns:
        Absolute path to the best available font, or None.
    """
    # Try candidates first (exact names from the preset)
    for candidate in _FONT_CANDIDATES.get(font_key, []):
        found = _find_font_by_name(candidate.lower(), all_fonts)
        if found:
            return found
    # Try fallback chain
    for fallback in _FONT_FALLBACKS.get(font_key, []):
        found = _find_font_by_name(fallback.lower(), all_fonts)
        if found:
            return found
    return None


# Discover fonts once at import time
_SYSTEM_FONT_DIRS = _get_font_dirs()
_ALL_SYSTEM_FONTS = _find_fonts_in_dirs(_SYSTEM_FONT_DIRS)

# Resolve key fonts
_IMPACT = _resolve_font("impact", _ALL_SYSTEM_FONTS)
_ARIAL = _resolve_font("arial", _ALL_SYSTEM_FONTS)
_ARIAL_BOLD = _resolve_font("arial_bold", _ALL_SYSTEM_FONTS)
_SEGOE_UI = _resolve_font("segoe_ui", _ALL_SYSTEM_FONTS)
_SEGOE_UI_BOLD = _resolve_font("segoe_ui_bold", _ALL_SYSTEM_FONTS)
_GEORGIA = _resolve_font("georgia", _ALL_SYSTEM_FONTS)

# Ultimate fallback: first font we found, or empty string (Pillow will use default)
_FALLBACK_FONT = _ALL_SYSTEM_FONTS[0] if _ALL_SYSTEM_FONTS else ""

if not _IMPACT:
    _IMPACT = _FALLBACK_FONT
    if _IMPACT:
        logger.info(f"Impact font not found, using fallback: {os.path.basename(_IMPACT)}")


def get_system_fonts():
    """Return the list of all discovered system fonts (for dropdown menus)."""
    return list(_ALL_SYSTEM_FONTS) if _ALL_SYSTEM_FONTS else [_FALLBACK_FONT or ""]


DEFAULT_STYLE = {
    "font_path": _IMPACT or "",
    "font_size": 120,
    "fill_color": "#FFFFFF",
    "highlight_colors": ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
    "stroke_color": "#000000",
    "stroke_width": 8,
    "shadow_color": "#000000",
    "shadow_opacity": 0.6,
    "shadow_offset_x": 4,
    "shadow_offset_y": 4,
    "shadow_blur": 6.0,
    "anim_duration": 0.15,
    "y_position": 0.45,
    "uppercase": True,
}

STYLE_PRESETS = {
    "Hormozi": {
        "font_path": _IMPACT,
        "font_size": 120,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
        "stroke_color": "#000000",
        "stroke_width": 8,
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,
        "shadow_offset_x": 4,
        "shadow_offset_y": 4,
        "shadow_blur": 6.0,
        "anim_duration": 0.15,
        "y_position": 0.45,
        "uppercase": True,
    },
    "MrBeast": {
        "font_path": _IMPACT,
        "font_size": 160,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#FFD700", "#FF4444", "#00FF00", "#FFFFFF"],
        "stroke_color": "#000000",
        "stroke_width": 12,
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,
        "shadow_offset_x": 4,
        "shadow_offset_y": 4,
        "shadow_blur": 8.0,
        "anim_duration": 0.12,
        "y_position": 0.42,
        "uppercase": True,
    },
    "Clean": {
        "font_path": _SEGOE_UI or _ARIAL or _IMPACT,
        "font_size": 72,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#87CEEB", "#90EE90", "#FFFF99", "#FFB6C1"],
        "stroke_color": "#000000",
        "stroke_width": 0,
        "shadow_color": "#000000",
        "shadow_opacity": 0.5,
        "shadow_offset_x": 0,
        "shadow_offset_y": 0,
        "shadow_blur": 12.0,
        "anim_duration": 0.0,
        "y_position": 0.75,
        "uppercase": False,
    },
    "Netflix": {
        "font_path": _ARIAL or _IMPACT,
        "font_size": 64,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#FFFFFF", "#FFFFFF", "#FFFFFF", "#FFFFFF"],
        "stroke_color": "#000000",
        "stroke_width": 0,
        "shadow_color": "#000000",
        "shadow_opacity": 0.5,
        "shadow_offset_x": 0,
        "shadow_offset_y": 0,
        "shadow_blur": 4.0,
        "anim_duration": 0.0,
        "y_position": 0.88,
        "uppercase": False,
    },
    "Neon": {
        "font_path": _IMPACT,
        "font_size": 120,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#FF00FF", "#00FFFF", "#39FF14", "#FF6600"],
        "stroke_color": "#2D0040",
        "stroke_width": 6,
        "shadow_color": "#8800FF",
        "shadow_opacity": 0.7,
        "shadow_offset_x": 0,
        "shadow_offset_y": 0,
        "shadow_blur": 16.0,
        "anim_duration": 0.15,
        "y_position": 0.45,
        "uppercase": True,
    },
    "Pop Color": {
        "font_path": _ARIAL_BOLD or _IMPACT,
        "font_size": 110,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#FF69B4", "#4488FF", "#FFD700", "#00CC66"],
        "stroke_color": "#000000",
        "stroke_width": 10,
        "shadow_color": "#000000",
        "shadow_opacity": 0.6,
        "shadow_offset_x": 4,
        "shadow_offset_y": 4,
        "shadow_blur": 6.0,
        "anim_duration": 0.18,
        "y_position": 0.45,
        "uppercase": True,
    },
    "Podcast": {
        "font_path": _SEGOE_UI_BOLD or _ARIAL_BOLD or _IMPACT,
        "font_size": 80,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#6699CC", "#66AA66", "#CC8844", "#CC6644"],
        "stroke_color": "#333333",
        "stroke_width": 4,
        "shadow_color": "#000000",
        "shadow_opacity": 0.5,
        "shadow_offset_x": 2,
        "shadow_offset_y": 2,
        "shadow_blur": 8.0,
        "anim_duration": 0.0,
        "y_position": 0.80,
        "uppercase": False,
    },
    "Luxury": {
        "font_path": _GEORGIA or _IMPACT,
        "font_size": 90,
        "fill_color": "#FFFFFF",
        "highlight_colors": ["#FFD700", "#FFFDD0", "#F5DEB3", "#FFFFFF"],
        "stroke_color": "#1A1A1A",
        "stroke_width": 2,
        "shadow_color": "#000000",
        "shadow_opacity": 0.5,
        "shadow_offset_x": 2,
        "shadow_offset_y": 2,
        "shadow_blur": 10.0,
        "anim_duration": 0.10,
        "y_position": 0.50,
        "uppercase": True,
    },
}

PRESET_NAMES = ["Custom"] + list(STYLE_PRESETS.keys())


def _resolve_preset_font(preset_style: dict) -> dict:
    """Return a copy of the preset style with font fallback if the path is missing."""
    style = dict(preset_style)
    if not style.get("font_path") or not os.path.isfile(style["font_path"]):
        style["font_path"] = _IMPACT or _FALLBACK_FONT or ""
    return style


def hex_to_rgb(hex_color: str) -> tuple:
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def hex_to_rgba(hex_color: str, alpha: float = 1.0) -> tuple:
    """Convert '#RRGGBB' + alpha to (R, G, B, A) tuple (0-255 each)."""
    r, g, b = hex_to_rgb(hex_color)
    return (r, g, b, int(alpha * 255))
