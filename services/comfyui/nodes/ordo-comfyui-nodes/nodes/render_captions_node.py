"""
Ordo Render Captions — overlay animated captions onto image frames using Pillow.

Three-layer rendering per word: shadow (blurred) → stroke (outline) → fill (colored).
Back-ease-out pop-in animation, word highlighting with cycling colors.
Alpha compositing onto video frames via numpy.

Supports multi-track captions and per-word style overrides.
"""

import gc
import logging
import math

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from .caption_types import DEFAULT_STYLE, hex_to_rgb, hex_to_rgba

logger = logging.getLogger("Ordo")


class OrdoRenderCaptionsNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "Batch of video frames (B, H, W, 3) float32 0-1"}),
                "word_timings": ("ORDO_WORD_TIMINGS", {"tooltip": "Word-level timestamps from Transcribe or TTS"}),
                "fps": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 120.0, "step": 0.01,
                                  "tooltip": "Frames per second of the video"}),
                "max_words": ("INT", {"default": 3, "min": 1, "max": 10, "step": 1,
                                      "tooltip": "Maximum words per caption group"}),
                "pause_threshold": ("FLOAT", {"default": 0.3, "min": 0.05, "max": 2.0, "step": 0.05,
                                              "tooltip": "Gap (seconds) between words that forces a new group"}),
            },
            "optional": {
                "style": ("ORDO_CAPTION_STYLE", {"tooltip": "Visual style from Caption Style node (uses defaults if not connected)"}),
                "time_offset": ("FLOAT", {"default": 0.0, "min": -60.0, "max": 60.0, "step": 0.05,
                                          "tooltip": "Shift all word timings ±N seconds"}),
                "speed_factor": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 5.0, "step": 0.05,
                                           "tooltip": "Scale timings (>1=compress, <1=stretch, anchored to first word)"}),
                "word_padding_before": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.01,
                                                  "tooltip": "Extend each word's highlight start earlier by N seconds"}),
                "word_padding_after": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 2.0, "step": 0.01,
                                                 "tooltip": "Extend each word's highlight end later by N seconds"}),
                "track_spacing": ("FLOAT", {"default": 0.12, "min": 0.0, "max": 0.5, "step": 0.01,
                                            "tooltip": "Vertical spacing between tracks (fraction of frame height)"}),
                "bypass": ("BOOLEAN", {"default": False,
                                       "tooltip": "True = skip caption rendering and pass frames through unchanged"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "render"
    CATEGORY = "Ordo"
    DESCRIPTION = "Overlay animated Hormozi/CapCut-style captions onto video frames"

    def render(self, images, word_timings, fps=30.0, max_words=3,
               pause_threshold=0.3, style=None, time_offset=0.0,
               speed_factor=1.0, word_padding_before=0.0, word_padding_after=0.0,
               track_spacing=0.12, bypass=False):
        if bypass:
            return (images,)
        if style is None:
            style = dict(DEFAULT_STYLE)

        # images: torch tensor [B, H, W, 3] float 0-1
        B, H, W, _ = images.shape

        # Font cache shared across all layout/render calls
        font_cache = {}

        # Group words (applies speed_factor, time_offset, padding, then segments)
        word_timings = self._group_words(
            word_timings, max_words, pause_threshold,
            time_offset, speed_factor, word_padding_before, word_padding_after
        )

        # Partition words by track
        tracks = {}
        for wt in word_timings:
            track_id = wt.get("track", 0)
            if track_id not in tracks:
                tracks[track_id] = []
            tracks[track_id].append(wt)

        # Build segments and layouts per track
        track_data = {}  # {track_id: {"segments": [...], "layouts": [...]}}
        for track_id in sorted(tracks.keys()):
            track_words = tracks[track_id]
            segments = self._build_segments(track_words)
            if not segments:
                continue

            seg_layouts = []
            for seg in segments:
                layout = self._layout_segment(seg, style, font_cache, W, H)
                seg_layouts.append(layout)

            track_data[track_id] = {"segments": segments, "layouts": seg_layouts}

        if not track_data:
            logger.warning("No caption segments to render (word_timings may be empty or ungrouped)")
            return (images,)

        total_segments = sum(len(td["segments"]) for td in track_data.values())
        logger.info(f"Rendering captions on {B} frames at {H}x{W} ({total_segments} segments across {len(track_data)} tracks)")

        # Base y_position from style
        base_y = style.get("y_position", DEFAULT_STYLE["y_position"])

        # Process frames in place on the input tensor. ComfyUI doesn't defensively
        # clone node inputs, and our only downstream consumer (logo overlay) is the
        # sole reader, so mutating + returning `images` saves a 17 GB-class duplicate
        # batch that would otherwise sit alive until the workflow ends.
        result = images
        for frame_idx in range(B):
            t = frame_idx / fps

            # Collect overlays from all tracks
            overlays = []
            for track_id in sorted(track_data.keys()):
                td = track_data[track_id]
                segments = td["segments"]
                layouts = td["layouts"]

                # Find active segment for this track
                active_seg_idx = None
                for si, seg in enumerate(segments):
                    if seg["start"] <= t <= seg["end"]:
                        active_seg_idx = si
                        break

                if active_seg_idx is not None:
                    layout = layouts[active_seg_idx]
                    # Skip if all words are hidden
                    if not layout["words_info"]:
                        continue

                    # Compute Y position for this track
                    y_pos = base_y + track_id * track_spacing
                    y_pos = min(0.95, y_pos)
                    if y_pos >= 0.95 and track_id > 0:
                        logger.warning(f"Track {track_id} Y position clamped to 0.95 (off-screen)")

                    overlay = self._render_segment_overlay(
                        t, segments[active_seg_idx], layout,
                        style, font_cache, W, H, y_pos
                    )
                    overlays.append(overlay)

            if overlays:
                frame_np = (images[frame_idx].cpu().numpy() * 255).astype(np.uint8)
                frame_pil = Image.fromarray(frame_np, "RGB").convert("RGBA")

                # Composite all track overlays (track 0 first, higher tracks on top)
                composited = frame_pil
                for overlay in overlays:
                    new_composited = Image.alpha_composite(composited, overlay)
                    composited.close()
                    composited = new_composited
                    overlay.close()
                frame_pil.close()
                frame_pil = composited.convert("RGB")
                composited.close()
                frame_np = np.asarray(frame_pil, dtype=np.float32) / 255.0
                result[frame_idx] = torch.from_numpy(frame_np)
                frame_pil.close()
                del frame_np, frame_pil, overlays
            else:
                result[frame_idx] = images[frame_idx]

            # Force-collect every 100 frames to release PIL buffers that
            # Python's gc would otherwise leave for later (the per-frame
            # working set is ~5 MB but accumulates if gc lags).
            if (frame_idx + 1) % 100 == 0:
                gc.collect()

        gc.collect()
        return (result,)

    # ── Word grouping ────────────────────────────────────────────────────

    def _group_words(self, word_timings, max_words, pause_threshold,
                     time_offset, speed_factor,
                     word_padding_before, word_padding_after):
        """Apply timing adjustments and group words into caption segments.

        Steps: speed_factor → time_offset → word padding → segment grouping.
        Preserves all extra fields (track, overrides) via dict copy.
        Groups are computed per-track so words on different tracks aren't merged.
        """
        if not word_timings:
            return word_timings

        # 1. Apply speed_factor (anchor to first word's start)
        anchor = word_timings[0]["start"]
        adjusted = []
        for wt in word_timings:
            entry = dict(wt)  # preserve ALL fields (track, overrides, etc.)
            s = anchor + (wt["start"] - anchor) / speed_factor
            e = anchor + (wt["end"] - anchor) / speed_factor
            entry["start"] = s
            entry["end"] = e
            adjusted.append(entry)

        # 2. Apply time_offset
        for wt in adjusted:
            wt["start"] += time_offset
            wt["end"] += time_offset

        # 3. Apply word padding (clamp start >= 0)
        for wt in adjusted:
            wt["start"] = max(0.0, wt["start"] - word_padding_before)
            wt["end"] = wt["end"] + word_padding_after

        # 4. Partition by track, then group into segments per track
        by_track = {}
        for wt in adjusted:
            track_id = wt.get("track", 0)
            if track_id not in by_track:
                by_track[track_id] = []
            by_track[track_id].append(wt)

        grouped = []
        seg_idx = 0

        for track_id in sorted(by_track.keys()):
            track_words = by_track[track_id]
            current_count = 0

            for i, wt in enumerate(track_words):
                if i == 0:
                    current_count = 1
                else:
                    gap = wt["start"] - track_words[i - 1]["end"]
                    if current_count >= max_words or gap > pause_threshold:
                        seg_idx += 1
                        current_count = 1
                    else:
                        current_count += 1

                wt["segment_idx"] = seg_idx
                grouped.append(wt)

            # Increment seg_idx for next track's words
            if track_words:
                seg_idx += 1

        logger.info(f"Grouped {len(grouped)} words into {seg_idx} caption segments")
        return grouped

    # ── Segment building ─────────────────────────────────────────────────

    def _build_segments(self, word_timings):
        """Build segments from word_timings grouped by segment_idx."""
        if not word_timings:
            return []

        seg_map = {}
        for wt in word_timings:
            idx = wt.get("segment_idx", 0)
            if idx not in seg_map:
                seg_map[idx] = []
            seg_map[idx].append(wt)

        segments = []
        for idx in sorted(seg_map.keys()):
            words = seg_map[idx]
            segments.append({
                "words": words,
                "start": words[0]["start"],
                "end": words[-1]["end"],
            })

        return segments

    # ── Font loading with cache ───────────────────────────────────────────

    def _get_font(self, style, font_cache, font_size=None):
        """Get a font from the cache, loading it if necessary."""
        font_path = style.get("font_path", DEFAULT_STYLE["font_path"])
        size = font_size or style.get("font_size", DEFAULT_STYLE["font_size"])
        key = (font_path, size)
        if key not in font_cache:
            try:
                font_cache[key] = ImageFont.truetype(font_path, size)
            except Exception:
                logger.warning(f"Could not load font {font_path}, using default")
                font_cache[key] = ImageFont.load_default()
        return font_cache[key]

    # ── Layout pre-computation ────────────────────────────────────────────

    def _layout_segment(self, seg, style, font_cache, W, H):
        """Pre-compute word positions for a segment, respecting per-word overrides."""
        uppercase = style.get("uppercase", True)
        base_font_size = style.get("font_size", DEFAULT_STYLE["font_size"])

        words_info = []
        for wt in seg["words"]:
            # Skip hidden words
            if wt.get("override_hidden", False):
                continue

            # Display text: override_text or original word
            raw_text = wt.get("override_text") or wt["word"]
            text = raw_text.upper() if uppercase else raw_text

            # Effective font size: override_font_size takes priority, then scale, then base
            override_font_size = wt.get("override_font_size")
            override_scale = wt.get("override_scale")
            if override_font_size:
                effective_size = int(override_font_size)
            elif override_scale:
                effective_size = int(base_font_size * override_scale)
            else:
                effective_size = base_font_size

            # Get font for this word
            word_font = self._get_font(style, font_cache, effective_size)

            bbox = word_font.getbbox(text)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]

            # Get ascent for baseline alignment
            ascent = word_font.getmetrics()[0]

            words_info.append({
                "text": text,
                "w": w,
                "h": h,
                "timing": wt,
                "font": word_font,
                "effective_size": effective_size,
                "ascent": ascent,
            })

        if not words_info:
            return {"words_info": [], "space_w": 0, "total_w": 0, "fit_scale": 1.0}

        # Measure space width using the first word's font
        sp_bbox = words_info[0]["font"].getbbox(" ")
        space_w = sp_bbox[2] - sp_bbox[0] if sp_bbox else base_font_size // 4

        # Total width
        total_w = sum(wi["w"] for wi in words_info) + space_w * max(0, len(words_info) - 1)

        # Fit scale (shrink if wider than 90% of frame)
        max_width = W * 0.9
        fit_scale = min(1.0, max_width / total_w) if total_w > 0 else 1.0

        return {
            "words_info": words_info,
            "space_w": space_w,
            "total_w": total_w,
            "fit_scale": fit_scale,
        }

    # ── Overlay rendering ─────────────────────────────────────────────────

    def _render_segment_overlay(self, t, seg, layout, style, font_cache, W, H, y_pos):
        """Render a single segment as an RGBA overlay image."""
        anim_duration = style.get("anim_duration", DEFAULT_STYLE["anim_duration"])
        fill_color_hex = style.get("fill_color", DEFAULT_STYLE["fill_color"])
        highlight_colors = style.get("highlight_colors", DEFAULT_STYLE["highlight_colors"])
        stroke_color_hex = style.get("stroke_color", DEFAULT_STYLE["stroke_color"])
        stroke_width = style.get("stroke_width", DEFAULT_STYLE["stroke_width"])
        shadow_color_hex = style.get("shadow_color", DEFAULT_STYLE["shadow_color"])
        shadow_opacity = style.get("shadow_opacity", DEFAULT_STYLE["shadow_opacity"])
        shadow_blur = style.get("shadow_blur", DEFAULT_STYLE["shadow_blur"])

        # Pop-in animation scale
        progress = min(1.0, (t - seg["start"]) / anim_duration) if anim_duration > 0 else 1.0
        scale = self._back_ease_out(progress)

        # Combined scale (animation * fit)
        total_scale = scale * layout["fit_scale"]

        # Find active word index (which word is currently being spoken)
        active_word_idx = 0
        for i, wi in enumerate(layout["words_info"]):
            if wi["timing"]["start"] <= t:
                active_word_idx = i

        # Compute max stroke width across all words (for padding)
        max_stroke = stroke_width
        for wi in layout["words_info"]:
            word_sw = wi["timing"].get("override_stroke_width")
            if word_sw is not None:
                max_stroke = max(max_stroke, word_sw)

        # Max ascent for baseline alignment
        max_ascent = max(wi["ascent"] for wi in layout["words_info"])

        # We render at natural size then scale the overlay for crispness
        pad = int(max_stroke * 2 + shadow_blur * 2 + 20)
        text_w = int(layout["total_w"])
        text_h = int(max(wi["h"] for wi in layout["words_info"]) + max_ascent * 0.3)
        overlay_w = text_w + pad * 2
        overlay_h = text_h + pad * 2

        # Create layers
        shadow_layer = Image.new("RGBA", (overlay_w, overlay_h), (0, 0, 0, 0))
        stroke_layer = Image.new("RGBA", (overlay_w, overlay_h), (0, 0, 0, 0))
        fill_layer = Image.new("RGBA", (overlay_w, overlay_h), (0, 0, 0, 0))

        shadow_draw = ImageDraw.Draw(shadow_layer)
        stroke_draw = ImageDraw.Draw(stroke_layer)
        fill_draw = ImageDraw.Draw(fill_layer)

        # Draw each word
        x = pad
        shadow_rgb = hex_to_rgb(shadow_color_hex)
        shadow_alpha = int(shadow_opacity * 255)
        fill_rgb = hex_to_rgb(fill_color_hex)
        stroke_rgb = hex_to_rgb(stroke_color_hex)

        sx, sy = style.get("shadow_offset_x", 4), style.get("shadow_offset_y", 4)

        for i, wi in enumerate(layout["words_info"]):
            text = wi["text"]
            wt = wi["timing"]
            word_font = wi["font"]

            # Baseline alignment: offset Y so all words align at the bottom
            baseline_y = pad + (max_ascent - wi["ascent"])

            # Per-word fill color
            word_override_color = wt.get("override_color")
            if word_override_color:
                inactive_rgb = hex_to_rgb(word_override_color)
            else:
                inactive_rgb = fill_rgb

            # Per-word highlight color
            word_override_highlight = wt.get("override_highlight")

            # Choose color based on active state
            if i == active_word_idx:
                if word_override_highlight:
                    word_rgb = hex_to_rgb(word_override_highlight)
                else:
                    color_idx = hash(wt["word"].lower()) & 0x7FFFFFFF
                    h_color = highlight_colors[color_idx % len(highlight_colors)]
                    word_rgb = hex_to_rgb(h_color)
            else:
                word_rgb = inactive_rgb

            # Per-word stroke
            word_stroke_width = wt.get("override_stroke_width", stroke_width)
            word_stroke_color_hex = wt.get("override_stroke_color", stroke_color_hex)
            word_stroke_rgb = hex_to_rgb(word_stroke_color_hex)

            # Shadow
            shadow_draw.text((x + sx, baseline_y + sy), text, font=word_font,
                             fill=(*shadow_rgb, shadow_alpha))

            # Stroke (draw with stroke_width)
            if word_stroke_width > 0:
                stroke_draw.text((x, baseline_y), text, font=word_font,
                                 fill=(*word_stroke_rgb, 255),
                                 stroke_width=word_stroke_width,
                                 stroke_fill=(*word_stroke_rgb, 255))

            # Fill
            fill_draw.text((x, baseline_y), text, font=word_font,
                           fill=(*word_rgb, 255))

            x += wi["w"] + layout["space_w"]

        # Blur shadow
        if shadow_blur > 0:
            blurred_shadow = shadow_layer.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
            shadow_layer.close()
            shadow_layer = blurred_shadow

        # Composite layers: shadow -> stroke -> fill
        ss = Image.alpha_composite(shadow_layer, stroke_layer)
        shadow_layer.close()
        stroke_layer.close()
        combined = Image.alpha_composite(ss, fill_layer)
        ss.close()
        fill_layer.close()

        # Scale the combined text overlay
        if total_scale != 1.0:
            new_w = max(1, int(overlay_w * total_scale))
            new_h = max(1, int(overlay_h * total_scale))
            scaled = combined.resize((new_w, new_h), Image.LANCZOS)
            combined.close()
            combined = scaled
        else:
            new_w, new_h = overlay_w, overlay_h

        # Place on full-size frame overlay
        frame_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        cx = (W - new_w) // 2
        cy = int(H * y_pos) - new_h // 2
        frame_overlay.paste(combined, (cx, cy))
        combined.close()

        return frame_overlay

    # ── Easing ────────────────────────────────────────────────────────────

    @staticmethod
    def _back_ease_out(t):
        """Back ease-out: overshoots to ~112% then settles to 100%."""
        c1 = 1.70158
        c3 = c1 + 1
        return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)
