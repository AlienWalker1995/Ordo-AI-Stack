#!/usr/bin/env python3
"""Measure every synthesized dialogue line of recent Rick & Morty reels.

Runs INSIDE ordo-comfyui-1 (has faster_whisper + resemblyzer, same as the render gate):
    python3 audit_lines.py --since 2026-09-08 --out /root/ComfyUI/output/rm_line_audit.json

For each /root/ComfyUI/output/draft_<slug>/line_XX.wav newer than --since, with the intended
text from /root/ComfyUI/input/rickmorty/dialogue_<slug>.json, it records:
  ratio / recall  - the render gate's own word metrics (whisper small.en, CPU int8)
  heard           - the transcript
  sim_own/sim_other - resemblyzer cosine vs the speaker's ref and vs the other ref
  dur_s, rms_db, peak_db, lead_silence_s, trail_silence_s
  attempts        - how many rmline_*.flac VibeVoice attempts fall inside the render window
Prints a table and writes JSON. No GPU use.
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import time

OUT = "/root/ComfyUI/output"
ASSETS = "/root/ComfyUI/input/rickmorty"
STOP = frozenset("""a an and are as at be but by for from has have he her his i if in is it its me my
no nor not of on or our she so that the their them then there these they this to too up was we were
what when which who will with you your aw geez""".split())


def tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", path], capture_output=True, text=True)
    dur = float((r.stdout or "0").strip() or 0)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    rms = peak = None
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            rms = float(line.split("mean_volume:")[1].split("dB")[0])
        if "max_volume" in line:
            peak = float(line.split("max_volume:")[1].split("dB")[0])
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af",
                        "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"],
                       capture_output=True, text=True)
    lead = trail = 0.0
    starts, ends = [], []
    for line in r.stderr.splitlines():
        if "silence_start" in line:
            starts.append(float(line.split("silence_start:")[1].split()[0]))
        if "silence_end" in line:
            ends.append(float(line.split("silence_end:")[1].split()[0]))
    if starts and starts[0] <= 0.05 and ends:
        lead = ends[0]
    if starts and (not ends or len(starts) > len(ends) or starts[-1] > ends[-1]):
        trail = max(0.0, dur - starts[-1])
    return {"dur_s": round(dur, 2), "rms_db": rms, "peak_db": peak,
            "lead_silence_s": round(lead, 2), "trail_silence_s": round(trail, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD; only drafts modified after this")
    ap.add_argument("--out", default=f"{OUT}/rm_line_audit.json")
    args = ap.parse_args()
    since = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    import numpy as np
    from faster_whisper import WhisperModel
    from resemblyzer import VoiceEncoder, preprocess_wav
    whisper = WhisperModel("small.en", device="cpu", compute_type="int8")
    enc = VoiceEncoder("cpu")
    refs = {s: enc.embed_utterance(preprocess_wav(f"{ASSETS}/{s}.wav")) for s in ("rick", "morty")}
    ref_cross = float(np.dot(refs["rick"], refs["morty"]))

    attempts_all = sorted((os.path.getmtime(p), p) for p in glob.glob(f"{OUT}/rmline_*.flac"))
    rows = []
    for d in sorted(glob.glob(f"{OUT}/draft_*")):
        if os.path.getmtime(d) < since or not os.path.isfile(f"{d}/line_00.wav"):
            continue
        slug = os.path.basename(d)[len("draft_"):]
        dj = f"{ASSETS}/dialogue_{slug}.json"
        if not os.path.isfile(dj):
            continue
        lines = json.load(open(dj)).get("lines", [])
        wavs = sorted(glob.glob(f"{d}/line_*.wav"))
        t0 = min(os.path.getmtime(w) for w in wavs) - 900
        t1 = max(os.path.getmtime(w) for w in wavs) + 60
        n_attempts = sum(1 for m, _ in attempts_all if t0 <= m <= t1)
        for i, w in enumerate(wavs):
            if i >= len(lines):
                break
            spk = lines[i]["speaker"]
            text = lines[i]["text"]
            segs, _ = whisper.transcribe(w)
            heard = " ".join(s.text for s in segs).strip()
            ratio = difflib.SequenceMatcher(None, tok(text), tok(heard)).ratio()
            content = [x for x in tok(text) if x not in STOP]
            hs = set(tok(heard))
            recall = (sum(1 for x in content if x in hs) / len(content)) if content else 1.0
            emb = enc.embed_utterance(preprocess_wav(w))
            other = "morty" if spk == "rick" else "rick"
            row = {"slug": slug, "line": i, "speaker": spk, "text": text, "heard": heard,
                   "ratio": round(ratio, 3), "recall": round(recall, 3),
                   "sim_own": round(float(np.dot(emb, refs[spk])), 3),
                   "sim_other": round(float(np.dot(emb, refs[other])), 3),
                   "attempts_in_render": n_attempts, "wav": w, **probe(w)}
            rows.append(row)
            print(f"{slug[:28]:28} L{i} {spk:5} ratio={ratio:.2f} recall={recall:.2f} "
                  f"own={row['sim_own']:.2f} other={row['sim_other']:.2f} dur={row['dur_s']:5.2f} "
                  f"rms={row['rms_db']} lead={row['lead_silence_s']} trail={row['trail_silence_s']} "
                  f"| {heard[:60]!r}", flush=True)
    # Every VibeVoice attempt (kept or discarded) inside each render window, scored the same
    # way and matched to its most likely line by transcript similarity. This is the only way
    # to see what the gate rejected: the endpoint drops the script's stderr.
    attempts = []
    seen_windows = {}
    for r in rows:
        seen_windows.setdefault(r["slug"], None)
    for slug in seen_windows:
        d = f"{OUT}/draft_{slug}"
        wavs = sorted(glob.glob(f"{d}/line_*.wav"))
        t0 = min(os.path.getmtime(w) for w in wavs) - 900
        t1 = max(os.path.getmtime(w) for w in wavs) + 60
        lines = json.load(open(f"{ASSETS}/dialogue_{slug}.json")).get("lines", [])
        for m, p in attempts_all:
            if not (t0 <= m <= t1):
                continue
            segs, _ = whisper.transcribe(p)
            heard = " ".join(s.text for s in segs).strip()
            best_i, best_r = -1, -1.0
            for i, ln in enumerate(lines):
                rr = difflib.SequenceMatcher(None, tok(ln["text"]), tok(heard)).ratio()
                if rr > best_r:
                    best_i, best_r = i, rr
            spk = lines[best_i]["speaker"] if best_i >= 0 else "?"
            emb = enc.embed_utterance(preprocess_wav(p))
            content = [x for x in tok(lines[best_i]["text"]) if x not in STOP] if best_i >= 0 else []
            hs = set(tok(heard))
            recall = (sum(1 for x in content if x in hs) / len(content)) if content else 1.0
            a = {"slug": slug, "file": os.path.basename(p), "mtime": time.strftime("%H:%M:%S", time.gmtime(m)),
                 "line_guess": best_i, "speaker": spk, "ratio": round(best_r, 3), "recall": round(recall, 3),
                 "sim_rick": round(float(np.dot(emb, refs["rick"])), 3),
                 "sim_morty": round(float(np.dot(emb, refs["morty"])), 3), "heard": heard, **probe(p)}
            attempts.append(a)
            print(f"  attempt {a['file']} {a['mtime']} -> L{best_i} {spk:5} ratio={best_r:.2f} "
                  f"recall={recall:.2f} rick={a['sim_rick']:.2f} morty={a['sim_morty']:.2f} "
                  f"dur={a['dur_s']:5.2f} | {heard[:60]!r}", flush=True)
    json.dump({"ref_cross_sim": round(ref_cross, 3), "rows": rows, "attempts": attempts},
              open(args.out, "w"), indent=1)
    print(f"ref rick-vs-morty sim={ref_cross:.3f}; wrote {len(rows)} line rows + "
          f"{len(attempts)} attempt rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
