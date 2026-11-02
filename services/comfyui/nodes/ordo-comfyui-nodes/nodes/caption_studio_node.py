"""
Ordo Caption Studio — unified node combining timing editor + style config + render.

Provides a single node with a React+Vite frontend that embeds inside the node's
DOM widget. The React app handles timeline editing, style configuration,
per-word overrides, and a canvas-based caption preview — all in one place.
"""

import base64
import io
import json
import logging
import math
import wave

import numpy as np
from PIL import Image

from .caption_types import (
    DEFAULT_STYLE,
    STYLE_PRESETS,
    _resolve_preset_font,
    get_system_fonts,
)
from .render_captions_node import OrdoRenderCaptionsNode

logger = logging.getLogger("Ordo")


class OrdoCaptionStudioNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {
                    "tooltip": "Batch of video frames (B, H, W, 3) float32 0-1",
                }),
                "word_timings": ("ORDO_WORD_TIMINGS", {
                    "tooltip": "Word-level timestamps from Transcribe or TTS",
                }),
                "fps": ("FLOAT", {
                    "default": 30.0, "min": 1.0, "max": 120.0, "step": 0.01,
                    "tooltip": "Frames per second of the video",
                }),
                "max_words": ("INT", {
                    "default": 3, "min": 1, "max": 10, "step": 1,
                    "tooltip": "Maximum words per caption group",
                }),
                "pause_threshold": ("FLOAT", {
                    "default": 0.3, "min": 0.05, "max": 2.0, "step": 0.05,
                    "tooltip": "Gap (seconds) between words that forces a new group",
                }),
            },
            "optional": {
                "time_offset": ("FLOAT", {
                    "default": 0.0, "min": -60.0, "max": 60.0, "step": 0.05,
                    "tooltip": "Shift all word timings ±N seconds",
                }),
                "speed_factor": ("FLOAT", {
                    "default": 1.0, "min": 0.1, "max": 5.0, "step": 0.05,
                    "tooltip": "Scale timings (>1=compress, <1=stretch)",
                }),
                "word_padding_before": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 2.0, "step": 0.01,
                    "tooltip": "Extend each word's highlight start earlier",
                }),
                "word_padding_after": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 2.0, "step": 0.01,
                    "tooltip": "Extend each word's highlight end later",
                }),
                "track_spacing": ("FLOAT", {
                    "default": 0.12, "min": 0.0, "max": 0.5, "step": 0.01,
                    "tooltip": "Vertical spacing between tracks",
                }),
                "audio": ("AUDIO", {
                    "tooltip": "Optional audio from TTS or Transcribe for synced preview playback",
                }),
            },
            "hidden": {
                "edited_timings": ("STRING", {"default": ""}),
                "style_config": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "Ordo"
    DESCRIPTION = "All-in-one caption studio: timeline editing, style config, preview, and rendering"

    def execute(self, images, word_timings, fps=30.0, max_words=3,
                pause_threshold=0.3, time_offset=0.0, speed_factor=1.0,
                word_padding_before=0.0, word_padding_after=0.0,
                track_spacing=0.12, audio=None, edited_timings="",
                style_config=""):

        # 1. Resolve style from React UI
        style = self._resolve_style(style_config)

        # 2. Resolve timings (edited from React UI or original)
        resolved_timings = self._resolve_timings(word_timings, edited_timings)

        # 3. Encode frame atlas for video scrubbing
        B, H, W, _ = images.shape
        frame_atlas = self._encode_frame_atlas(images, fps)

        # 4. Delegate rendering to existing pipeline
        renderer = OrdoRenderCaptionsNode()
        render_result = renderer.render(
            images=images,
            word_timings=resolved_timings,
            fps=fps,
            max_words=max_words,
            pause_threshold=pause_threshold,
            style=style,
            time_offset=time_offset,
            speed_factor=speed_factor,
            word_padding_before=word_padding_before,
            word_padding_after=word_padding_after,
            track_spacing=track_spacing,
        )
        rendered_images = render_result[0]

        # 5. Build UI output for React frontend
        font_list = get_system_fonts()

        # Build style presets dict for React
        style_presets = {}
        for name, preset in STYLE_PRESETS.items():
            style_presets[name] = dict(preset)

        # 6. Encode audio preview if provided
        audio_b64 = ""
        if audio is not None:
            try:
                audio_b64 = self._encode_audio_preview(audio)
                logger.info(f"Caption Studio: encoded audio preview ({len(audio_b64) // 1024}KB)")
            except Exception as e:
                logger.warning(f"Caption Studio: failed to encode audio — {e}")

        ui_data = {
            "timings_json": [json.dumps(word_timings)],
            "edited_timings_json": [edited_timings if edited_timings else ""],
            "style_json": [json.dumps(style)],
            "style_presets": [json.dumps(style_presets)],
            "fps": [fps],
            "frame_width": [W],
            "frame_height": [H],
            "frame_count": [B],
            "preview_frame": [""],
            "font_list": [json.dumps(font_list)],
            "frame_atlas": [json.dumps(frame_atlas)],
            "audio_data": [audio_b64],
        }

        logger.info(
            f"Caption Studio: rendered {B} frames, "
            f"{len(resolved_timings)} words, "
            f"{len(frame_atlas['frames'])} atlas frames, "
            f"style preset: {self._get_preset_name(style_config)}"
        )

        return {"ui": ui_data, "result": (rendered_images, audio)}

    def _resolve_style(self, style_config):
        """Parse JSON style config from React UI, merge with defaults."""
        if not style_config or not style_config.strip():
            return dict(DEFAULT_STYLE)

        try:
            config = json.loads(style_config)
            if not isinstance(config, dict):
                return dict(DEFAULT_STYLE)

            # Check for preset selection
            preset_name = config.get("_preset", "Custom")
            if preset_name != "Custom" and preset_name in STYLE_PRESETS:
                return _resolve_preset_font(STYLE_PRESETS[preset_name])

            # Build custom style, using defaults for missing keys
            style = dict(DEFAULT_STYLE)
            for key in DEFAULT_STYLE:
                if key in config:
                    style[key] = config[key]

            return style

        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Caption Studio: invalid style_config JSON — {e}")
            return dict(DEFAULT_STYLE)

    def _resolve_timings(self, original_timings, edited_timings):
        """Use edited timings if valid, otherwise return original."""
        if not edited_timings or not edited_timings.strip():
            return original_timings

        try:
            parsed = json.loads(edited_timings)
            if isinstance(parsed, list) and len(parsed) > 0:
                required_keys = {"word", "start", "end"}
                if all(required_keys.issubset(w.keys()) for w in parsed):
                    for w in parsed:
                        if "segment_idx" not in w:
                            w["segment_idx"] = -1
                        if "track" not in w:
                            w["track"] = 0
                    logger.info(
                        f"Caption Studio: using {len(parsed)} edited word timings"
                    )
                    return parsed
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(
                f"Caption Studio: invalid edited_timings JSON — {e}"
            )

        return original_timings

    def _encode_frame_atlas(self, images, fps):
        """Encode sampled frames as base64 JPEG array for React video scrubbing.

        Samples every Nth frame (targeting ~120 atlas frames max).
        Each frame resized to 480px wide, JPEG quality 60.
        Budget: ~120 frames × ~8KB = ~960KB.

        Returns: { frames: str[], interval: int, fps: float, width: int, height: int }
        """
        B, H, W, _ = images.shape
        max_atlas_frames = 120
        interval = max(1, math.ceil(B / max_atlas_frames))

        # Target width for atlas thumbnails
        target_w = 480
        ratio = target_w / W if W > target_w else 1.0
        target_h = int(H * ratio)

        frames = []
        for i in range(0, B, interval):
            try:
                frame_np = (images[i].cpu().numpy() * 255).astype(np.uint8)
                pil_img = Image.fromarray(frame_np, "RGB")

                if ratio < 1.0:
                    pil_img = pil_img.resize((target_w, target_h), Image.LANCZOS)

                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=60)
                frames.append(base64.b64encode(buf.getvalue()).decode("ascii"))
            except Exception as e:
                logger.warning(f"Caption Studio: failed to encode atlas frame {i} — {e}")
                frames.append("")

        actual_w = target_w if ratio < 1.0 else W
        actual_h = target_h if ratio < 1.0 else H

        return {
            "frames": frames,
            "interval": interval,
            "fps": float(fps),
            "width": actual_w,
            "height": actual_h,
        }

    def _encode_audio_preview(self, audio):
        """Encode AUDIO dict as base64 WAV (22kHz mono) for browser preview."""
        waveform = audio["waveform"][0]  # [channels, samples]
        sr = audio["sample_rate"]
        mono = waveform.mean(dim=0).cpu().numpy()  # [samples]

        # Resample to 22050 Hz for smaller payload
        target_sr = 22050
        if sr != target_sr:
            old_len = len(mono)
            new_len = int(old_len * target_sr / sr)
            mono = np.interp(
                np.linspace(0, old_len - 1, new_len),
                np.arange(old_len), mono
            )

        # Convert to 16-bit PCM WAV
        mono = np.clip(mono, -1.0, 1.0)
        pcm = (mono * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_sr)
            wf.writeframes(pcm.tobytes())

        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _get_preset_name(self, style_config):
        """Extract preset name from style config JSON."""
        if not style_config or not style_config.strip():
            return "Default"
        try:
            config = json.loads(style_config)
            return config.get("_preset", "Custom")
        except Exception:
            return "Custom"
