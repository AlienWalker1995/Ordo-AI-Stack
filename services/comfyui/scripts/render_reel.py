#!/usr/bin/env python3
"""
Render a brainrot-format Instagram Reel from a script.

Pipeline:
1. Kokoro TTS  -> narration.wav (24kHz mono, upsampled to 48kHz on encode)
2. faster-whisper word-level transcription -> word_captions.srt
3. Pick an unused 30s Subway Surfers gameplay segment from manifest
4. ffmpeg vstack compose -> 1080x1920 reel.mp4 with FontSize=140 karaoke captions

Inputs (via --input-json):
{
  "script": "spoken text",
  "voice": "am_michael",   # default
  "slug":  "topic-slug",   # used for output filename and draft folder
}

Output:
{
  "ok": true,
  "draft_dir": "/root/ComfyUI/output/draft_<slug>",
  "reel_path": "/root/ComfyUI/output/draft_<slug>/reel.mp4",
  "narration_sec": 22.20,
  "word_count": 49,
  "gameplay_segment": "subway_seg_0017.mp4",
  "size_bytes": 5234567
}

Exits non-zero with a JSON error blob to stdout on any failure.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys
import time

import soundfile as sf

OUT_BASE = "/root/ComfyUI/output"
INPUT_GAMEPLAY_DIR = "/root/ComfyUI/input/gameplay"
MANIFEST_PATH = "/root/ComfyUI/output/gameplay_manifest.json"

# Script-length guard: refuse to render if narration is going to overrun a 30s
# gameplay segment. Kokoro at speed=1 is ~3-4 words/sec. 75 words ≈ 22-25s.
MAX_WORDS = 80
MAX_NARRATION_SEC = 28.0


def die(msg: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": msg, **extra}), flush=True)
    sys.exit(1)


def load_manifest() -> dict:
    if not os.path.exists(MANIFEST_PATH):
        # First run — discover segments and seed the manifest.
        segs = sorted(glob.glob(f"{INPUT_GAMEPLAY_DIR}/subway_seg_*.mp4"))
        return {"used": [], "available": [os.path.basename(p) for p in segs]}
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def save_manifest(m: dict) -> None:
    with open(MANIFEST_PATH, "w") as f:
        json.dump(m, f, indent=2)


def pick_gameplay() -> str:
    m = load_manifest()
    available = [s for s in m.get("available", []) if s not in m.get("used", [])]
    if not available:
        # Wrap around — reset used list.
        available = m.get("available", [])
        m["used"] = []
    if not available:
        die("no gameplay segments in manifest", manifest_path=MANIFEST_PATH)
    seg = random.choice(available)
    m.setdefault("used", []).append(seg)
    save_manifest(m)
    return seg


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input-json", required=True, help="JSON with script/voice/slug")
    args = p.parse_args()

    try:
        req = json.loads(args.input_json)
    except json.JSONDecodeError as e:
        die(f"bad --input-json: {e}")

    script = (req.get("script") or "").strip()
    voice = req.get("voice", "am_michael")
    slug = req.get("slug") or f"reel_{int(time.time())}"

    if not script:
        die("script is required")

    word_count = len(script.split())
    if word_count > MAX_WORDS:
        die(f"script too long: {word_count} words (max {MAX_WORDS})",
            word_count=word_count, max_words=MAX_WORDS)

    draft = f"{OUT_BASE}/draft_{slug}"
    os.makedirs(draft, exist_ok=True)
    with open(f"{draft}/script.txt", "w") as f:
        f.write(script)

    t_total = time.time()

    # --- 1. Kokoro TTS ---
    import numpy as np
    from kokoro import KPipeline

    t0 = time.time()
    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    chunks = [a for _, _, a in pipeline(script, voice=voice, speed=1.0)]
    audio = np.concatenate(chunks)
    narration_sec = len(audio) / 24000
    wav_path = f"{draft}/narration.wav"
    sf.write(wav_path, audio, 24000)
    t_tts = time.time() - t0

    if narration_sec > MAX_NARRATION_SEC:
        die(f"narration too long: {narration_sec:.1f}s (max {MAX_NARRATION_SEC}s)",
            narration_sec=narration_sec, max_sec=MAX_NARRATION_SEC, word_count=word_count)

    # --- 2. faster-whisper word-level transcription ---
    from faster_whisper import WhisperModel

    t0 = time.time()
    model = WhisperModel("small.en", device="cuda", compute_type="float16")
    segments, _ = model.transcribe(wav_path, word_timestamps=True)
    words: list[tuple[float, float, str]] = []
    for s in segments:
        for w in s.words or []:
            words.append((w.start, w.end, w.word.strip()))
    if not words:
        die("transcription produced no words", narration_sec=narration_sec)
    t_stt = time.time() - t0

    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int(t // 60 % 60)
        s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    srt_path = f"{draft}/captions.srt"
    with open(srt_path, "w") as f:
        for i, (start, end, word) in enumerate(words, 1):
            f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{word.upper()}\n\n")

    # --- 3. Pick gameplay segment ---
    gameplay_name = pick_gameplay()
    gameplay_path = f"{INPUT_GAMEPLAY_DIR}/{gameplay_name}"
    if not os.path.exists(gameplay_path):
        die(f"picked gameplay segment missing: {gameplay_path}")

    # --- 4. ffmpeg compose ---
    t0 = time.time()
    reel_path = f"{draft}/reel.mp4"
    filter_complex = (
        f"color=c=0x0a0a0a:s=1080x960:d={narration_sec}[topbg];"
        f"[topbg]subtitles={srt_path}:force_style="
        f"'FontName=Arial Black,FontSize=140,Outline=8,OutlineColour=&H000000,"
        f"PrimaryColour=&H00FFFFFF,Bold=1,Alignment=2,MarginV=160'[top];"
        f"[1:v]scale=1080:960:force_original_aspect_ratio=increase,"
        f"crop=1080:960,setpts=PTS-STARTPTS[bot];"
        f"[top][bot]vstack=inputs=2[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", wav_path,
        "-stream_loop", "-1", "-i", gameplay_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a",
        "-t", f"{narration_sec:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        reel_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        die("ffmpeg failed", stderr=res.stderr[-1000:])
    t_compose = time.time() - t0

    size = os.path.getsize(reel_path)
    meta = {
        "voice": voice,
        "slug": slug,
        "script": script,
        "word_count": word_count,
        "narration_sec": round(narration_sec, 2),
        "gameplay_segment": gameplay_name,
        "reel_size_bytes": size,
        "timing_sec": {
            "tts": round(t_tts, 2),
            "transcribe": round(t_stt, 2),
            "compose": round(t_compose, 2),
            "total": round(time.time() - t_total, 2),
        },
    }
    with open(f"{draft}/meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(json.dumps({
        "ok": True,
        "draft_dir": draft,
        "reel_path": reel_path,
        **meta,
    }), flush=True)


if __name__ == "__main__":
    main()
