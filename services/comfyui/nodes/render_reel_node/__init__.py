"""Render Reel — ComfyUI custom node wrapping scripts/render_reel.py.

Single node, three inputs (script, voice, slug). Shells out to render_reel.py
inside the same container, captures the JSON envelope, returns the reel.mp4
path. Marked OUTPUT_NODE so the dashboard orchestration worker sees it as a
terminal artifact step.

Designed to be dispatched via the dashboard `/api/orchestration/run` endpoint
with `workflow_id="audio/render_reel"` and `params={"script","voice","slug"}` —
not invoked from the ComfyUI GUI directly.
"""

from __future__ import annotations

import json
import subprocess

SCRIPT_PATH = "/root/ComfyUI/scripts/render_reel.py"
OUTPUT_DIR = "/root/ComfyUI/output"


class RenderReel:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "script": ("STRING", {"multiline": True, "default": ""}),
                "voice": ("STRING", {"default": "am_michael"}),
                "slug": ("STRING", {"default": "reel"}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("reel_path", "meta_json")
    FUNCTION = "render"
    CATEGORY = "Ordo/Reel"
    OUTPUT_NODE = True

    def render(self, script: str, voice: str, slug: str):
        if not script.strip():
            raise ValueError("script is required")
        if not slug or not all(c.isalnum() or c in "_-" for c in slug):
            raise ValueError("slug must be alphanumeric (with _ or -)")

        payload = json.dumps({"script": script.strip(), "voice": voice, "slug": slug})
        proc = subprocess.run(
            ["python3", SCRIPT_PATH, "--input-json", payload],
            capture_output=True,
            text=True,
            timeout=300,
        )

        # render_reel.py prints exactly one JSON envelope on stdout (kokoro/torch
        # warnings hit stderr). Parse the last `{...}` line to tolerate any
        # straggler warnings that leaked to stdout.
        envelope = None
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    envelope = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if envelope is None or not envelope.get("ok"):
            err_tail = (proc.stderr or "")[-1000:]
            out_tail = (proc.stdout or "")[-1000:]
            raise RuntimeError(
                f"render_reel.py failed (exit {proc.returncode}). "
                f"stderr_tail={err_tail!r} stdout_tail={out_tail!r}"
            )

        # Container-side path; the orchestration job record will surface this
        # to the caller. The same file is visible on the host at
        # data/comfyui-output/draft_<slug>/reel.mp4 via the bind mount.
        reel_path = envelope["reel_path"]
        meta = {
            "voice": envelope.get("voice"),
            "slug": envelope.get("slug"),
            "narration_sec": envelope.get("narration_sec"),
            "word_count": envelope.get("word_count"),
            "gameplay_segment": envelope.get("gameplay_segment"),
            "reel_size_bytes": envelope.get("reel_size_bytes"),
            "timing_sec": envelope.get("timing_sec"),
        }
        return {
            "ui": {
                "reel_path": [reel_path],
                "meta": [json.dumps(meta)],
            },
            "result": (reel_path, json.dumps(meta)),
        }


NODE_CLASS_MAPPINGS = {"RenderReel": RenderReel}
NODE_DISPLAY_NAME_MAPPINGS = {"RenderReel": "Render Reel (Kokoro + ffmpeg)"}
