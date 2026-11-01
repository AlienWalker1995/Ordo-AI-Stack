#!/usr/bin/env python3
"""
Render a two-character (Rick <-> Morty style) AI-news brainrot Reel.

Extends render_reel.py to a *dialogue* format:
1. Per-line TTS (VibeVoice voice-clone per speaker, or a silent placeholder for
   fast visual testing) -> per-line wav + duration.
2. Insert a burp SFX after every line flagged `burp` (the Rick lines).
3. Concatenate -> full.wav, tracking per-speaker intervals + burp windows.
4. faster-whisper word-level transcription -> captions.srt (karaoke).
5. ffmpeg compose -> 1080x1920 reel.mp4:
     gameplay bg  +  concept cards across the top band (pre-rendered RGBA clips, one per
                     `concepts[]` entry, shown over the lines it names)
                  +  speaker avatar (Morty left / Rick right) swapped to the talker
                  +  Hormozi karaoke captions (mid-frame, under the card band)

Input: --dialogue-json points at a dialogue.json:
  { "slug": "...", "story": {"headline": "...", "source": "..."},
    "lines": [ {"speaker":"morty","text":"..."},
               {"speaker":"rick","text":"...","burp":true}, ... ],
    "concepts": [ {"lines": [0, 1], "template": "stat", "data": {...}}, ... ] }   (optional)

Concept cards: each `concepts[i]` is shown from the start of its first listed line to the end of
its last one (burp included). The CLIPS are not rendered here: the driver (render_publish_dialogue.py,
in the agent container) asks the html-video service for them and stages them as
<draft>/cards/card_NN.mov before it fires this render (see concept_cards.py there). A declared
card whose clip is missing is skipped with a warning, never fatal.

Assets (--assets-dir, default /root/ComfyUI/input/rickmorty):
  rick.wav morty.wav  (VibeVoice reference clips; placeholder-tts ignores them)
  rick.png morty.png  (transparent avatars, one per speaker)
  burp.wav            (burp SFX appended after Rick lines)

Prints a JSON result blob to stdout; exits non-zero with an error blob on failure.
LLM-free by design: safe to call from the render cron without an agent in the loop.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

from spoken_text import spoken  # sibling module in scripts/

OUT_BASE = "/root/ComfyUI/output"
INPUT_GAMEPLAY_DIR = "/root/ComfyUI/input/gameplay"
COMFY_API = os.environ.get("COMFY_API", "http://127.0.0.1:8188")
SPEAKERS = ("rick", "morty")

# VibeVoice cloning quality: full-precision Large preserves Rick's raspy voice —
# Q8 quantization degrades it (Morty's cleaner voice survives, Rick's doesn't).
# Large needs ~30GB, so it OOMs on the 32GB card unless comfyui is a FRESH process
# with llamacpp evicted: the render driver must restart comfyui + free the GPU
# first. `--vv-model VibeVoice-Large-Q8` is the reliable-but-lower-Rick fallback.
VV_MODEL = "VibeVoice-Large"
VV_STEPS = 40
# Per-line quality gate: VibeVoice occasionally garbles a line (deterministic per
# seed). After generating, transcribe it and compare to the intended text; if the
# match is poor, re-roll with the next seed. Fixed seed list keeps it reproducible.
VV_SEEDS = [11042, 42, 1042, 2042, 7, 3042, 4042, 5042, 6042]  # 11042 first: verified-clean Kroger line_00 take (2026-10-16)
VV_MATCH_MIN = 0.72
# Content-word recall gate: the line-level ratio above is too lenient for proper
# nouns — a 19-word line with 2-3 garbled words ("Cursor"->"quirk cursor",
# "Devin"->"death in two") still clears 0.72. Require that >= this fraction of the
# intended CONTENT words (non-stopwords) actually appear in the transcript. Catches
# a garbled name even when the rest of the line is clean.
VV_RECALL_MIN = 0.92
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have he her his i if in is it its me my
no nor not of on or our she so that the their them then there these they this to
too up was we were what when which who will with you your aw geez
""".split())
# Likeness gate: a line can transcribe perfectly yet drift off-voice (the 2026-08-13
# audit heard Morty lines that were audibly a different guy). One actor voices both
# characters, so a plain similarity to the speaker's reference barely separates them (the two
# references score 0.95 against each other). The gate is the MARGIN: a take must sound more
# like its own reference than the other character's. Calibrated 2026-10-15 on 234 shipped
# lines with WavLM-base-plus-sv: Rick's margin p5 is +0.027; 14 of 117 shipped Morty lines
# were closer to Rick (margin < 0).
VV_LIKENESS_MODEL = "microsoft/wavlm-base-plus-sv"
VV_LIKENESS_REVISION = "feb593a6c23c1cc3d9510425c29b0a14d2b07b1e"
VV_MARGIN_MIN = 0.01

# Frame layout (1080x1920), top to bottom:
#   y 90..650    concept-card band: a 1080x560 RGBA clip composited full-width (the card itself
#                sits inside it with its own margins). Clear of Instagram's top chrome.
#   y 660..      karaoke captions (ASS MarginV below), under the band, above the avatars' heads.
#   y 820..1460  avatars (Morty left / Rick right), name tags at 1440.
CARD_Y = 90
CARD_FADE_IN = 0.25
CARD_FADE_OUT = 0.30
AVATAR_FADE = 0.22  # fraction of the avatar's height, from the bottom, that fades to transparent
AVATAR_FADE_GEQ = (
    "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
    f"a='alpha(X,Y)*min(1,(H-Y)/(H*{AVATAR_FADE}))'"
)


def die(msg: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": msg, **extra}), flush=True)
    sys.exit(1)


def ff(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die("ffmpeg step failed", cmd=args[:6], stderr=r.stderr[-800:])


def audio_dur(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        die("ffprobe could not read duration", path=path, out=r.stdout[-200:])
    return 0.0


# --- loudness --------------------------------------------------------------

# Every line is levelled to one loudness so the speakers match each other (VibeVoice takes
# land anywhere from -21 to -25 LUFS), the burp sits just under the voices (the raw asset is
# ~8 LU louder than a line), and the finished mix is mastered to the short-form platforms'
# -14 LUFS with true peaks under -1.5 dBTP.
LINE_LUFS = -16.0
BURP_LUFS = -19.0
MASTER_LUFS = -14.0
MASTER_TRUE_PEAK = -1.5


def loudness_stats(path: str, target: float, true_peak: float) -> dict:
    """First loudnorm pass: the measured loudness of `path` against the given target."""
    r = subprocess.run(
        ["ffmpeg", "-nostats", "-i", path, "-af",
         f"loudnorm=I={target}:TP={true_peak}:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    report = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
    try:
        return json.loads(report)
    except json.JSONDecodeError:
        die("loudness measurement failed", path=path, stderr=r.stderr[-400:])
    return {}


def level_audio(src: str, dst: str, target: float, true_peak: float = -2.0) -> None:
    """Two-pass loudnorm to `target` LUFS. The second pass applies the measured values with
    linear=true, a single gain change, so the delivery is not reshaped the way one-pass
    loudnorm's dynamic mode reshapes it (and one pass also misses the target on short clips).
    Output stays 48 kHz mono; loudnorm alone would resample to 192 kHz."""
    m = loudness_stats(src, target, true_peak)
    ff("-i", src, "-af",
       f"loudnorm=I={target}:TP={true_peak}:LRA=11:linear=true:"
       f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
       f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
       f"offset={m['target_offset']}",
       "-ar", "48000", "-ac", "1", dst)


# --- TTS backends ----------------------------------------------------------

_KOK = {}
# Distinct Kokoro voices per speaker so the placeholder path is a *real* two-voice
# end-to-end test (real speech -> whisper captions -> compose), no heavy GPU.
_KOK_VOICE = {"rick": "am_michael", "morty": "af_bella"}


def synth_placeholder(text: str, speaker: str, out: str) -> None:
    """Real Kokoro speech (per-speaker voice) — stand-in for VibeVoice for testing."""
    import numpy as np
    import soundfile as sf
    if "pipe" not in _KOK:
        from kokoro import KPipeline
        _KOK["pipe"] = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    voice = _KOK_VOICE.get(speaker, "am_michael")
    chunks = [a for _, _, a in _KOK["pipe"](text, voice=voice, speed=1.0)]
    tmp = out + ".24k.wav"
    sf.write(tmp, np.concatenate(chunks), 24000)
    ff("-i", tmp, "-ar", "48000", "-ac", "1", out)
    os.remove(tmp)


def synth_vibevoice(text: str, ref_wav: str, out: str, seed: int = 42,
                    free_after: bool = False) -> None:
    """VibeVoiceSingleSpeakerNode via the ComfyUI API, cloning `ref_wav`.

    free_after: unload VibeVoice-Large after THIS line. Kept False across the per-line
    loop (the ~17-30GB model stays resident so lines 2..N skip the reload), but MUST be
    True on the final line — otherwise the model lingers in ComfyUI after the render and,
    when the GPU lease releases, llamacpp can't reload (OOM) and the agent's next call
    dies. VibeVoice owns its model outside ComfyUI's manager, so the generic /free can't
    drop it; free_memory_after_generate is its only real unload path."""
    ref_rel = os.path.relpath(ref_wav, "/root/ComfyUI/input")  # LoadAudio is input/-relative
    wf = {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": ref_rel}},
        "2": {"class_type": "VibeVoiceSingleSpeakerNode", "inputs": {
            "text": text, "model": VV_MODEL, "attention_type": "auto",
            "quantize_llm": "full precision", "free_memory_after_generate": free_after,
            "diffusion_steps": VV_STEPS, "seed": seed, "cfg_scale": 1.3,
            "use_sampling": False, "voice_to_clone": ["1", 0]}},
        "3": {"class_type": "SaveAudio", "inputs": {
            "audio": ["2", 0], "filename_prefix": "rmline"}},
    }
    body = json.dumps({"prompt": wf}).encode()
    # Guardian-cooperative: COMFYUI_SERIALIZE_LLAMACPP stops llamacpp when our job
    # hits the queue, but its ~2s poll can race the VibeVoice load -> OOM. On a
    # failed load, POST /free and retry; by the retry the guardian has freed the
    # card. No manual container juggling needed.
    last_pid = None
    for attempt in range(6):
        req = urllib.request.Request(f"{COMFY_API}/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        last_pid = json.load(urllib.request.urlopen(req, timeout=30))["prompt_id"]
        for _ in range(1800):  # LLM-free wait; render cron, no agent
            time.sleep(1.0)
            h = json.load(urllib.request.urlopen(f"{COMFY_API}/history/{last_pid}", timeout=15))
            if last_pid in h:
                outs = h[last_pid].get("outputs", {}).get("3", {}).get("audio", [])
                if outs:
                    fn = outs[0]["filename"]
                    sub = outs[0].get("subfolder", "")
                    src = os.path.join(OUT_BASE, sub, fn)
                    # 48k mono — MUST match burp + all other clips or the concat
                    # demuxer locks to this clip's rate and every segment desyncs.
                    ff("-i", src, "-ar", "48000", "-ac", "1", out)
                    return
                break  # finished with no audio (OOM/error) -> free + retry
        try:  # drop any half-loaded VibeVoice pool so the retry has a clean card
            urllib.request.urlopen(urllib.request.Request(
                f"{COMFY_API}/free", data=b'{"unload_models":true,"free_memory":true}',
                headers={"Content-Type": "application/json"}), timeout=15)
        except Exception:
            pass
        print(f"[vv] attempt {attempt + 1} produced no audio (guardian/VRAM race); "
              f"freed + waiting 15s to retry", file=sys.stderr, flush=True)
        time.sleep(15)
    die("vibevoice produced no audio after retries", prompt_id=last_pid)


_GATE_WHISPER = {}

# Whisper drops a line's first word when speech starts at the very first sample, which is how
# VibeVoice delivers a line ("Rick, Japan just paid..." was heard as "Japan just paid..."), so
# every line is transcribed with this much silence in front of it.
TRANSCRIBE_LEAD_IN = 0.5


def transcribe_line(model, wav: str, word_timestamps: bool = False):
    """Segments of `wav` as whisper hears it after a silent lead-in, with every time shifted
    back so they are relative to the start of `wav` itself."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav") as padded:
        ff("-i", wav, "-af", f"adelay={int(TRANSCRIBE_LEAD_IN * 1000)}:all=1",
           "-ar", "16000", "-ac", "1", padded.name)
        segs, _ = model.transcribe(padded.name, word_timestamps=word_timestamps)
        segs = list(segs)
    for seg in segs:
        for w in (seg.words or []):
            w.start = max(0.0, w.start - TRANSCRIBE_LEAD_IN)
            w.end = max(0.0, w.end - TRANSCRIBE_LEAD_IN)
    return segs


def _line_match(wav: str, intended: str) -> tuple[float, float, str]:
    """How closely a (CPU) recognizer's transcript of `wav` matches `intended` —
    a proxy for 'did VibeVoice say the right words cleanly?'. CPU keeps it off the
    GPU so it never contends with the resident VibeVoice model.

    Returns (ratio, recall, heard):
      ratio  — SequenceMatcher over ALL tokens (lenient; a few garbled words in a
               long line still clears VV_MATCH_MIN).
      recall — fraction of intended CONTENT words (non-stopwords) present in the
               transcript. Strict on proper nouns: a garbled name drops this below
               VV_RECALL_MIN even when the line-level ratio is fine.
    """
    import difflib
    import re
    if "m" not in _GATE_WHISPER:
        from faster_whisper import WhisperModel
        _GATE_WHISPER["m"] = WhisperModel("small.en", device="cpu", compute_type="int8")
    segs = transcribe_line(_GATE_WHISPER["m"], wav)
    heard = " ".join(s.text for s in segs)
    # Both sides go through the same speller, so whisper's formatting is not scored as a
    # missing word: "6" vs "six", "$300,000,000" vs "300 million dollars".
    tok = lambda s: re.findall(r"[a-z0-9']+", spoken(s).lower())
    ratio = difflib.SequenceMatcher(None, tok(intended), tok(heard)).ratio()
    heard_tokens = tok(heard)
    heard_set = set(heard_tokens)
    # A brand written as one word is often heard as two ("OpenAI" -> "open AI",
    # "ChatGPT" -> "chat GPT"), so a content word also counts when the transcript spells it
    # across neighbouring words.
    heard_joined = "".join(heard_tokens)
    content = [w for w in tok(intended) if w not in _STOPWORDS]
    if content:
        recall = sum(1 for w in content if w in heard_set or w in heard_joined) / len(content)
    else:
        recall = 1.0
    return ratio, recall, heard.strip()


_LIKENESS = {}


def _speaker_embedding(wav: str):
    """Unit-length WavLM x-vector of `wav` (CPU, 16 kHz mono)."""
    import librosa
    import soundfile as sf
    import torch
    if "model" not in _LIKENESS:
        from transformers import AutoFeatureExtractor, WavLMForXVector
        _LIKENESS["features"] = AutoFeatureExtractor.from_pretrained(
            VV_LIKENESS_MODEL, revision=VV_LIKENESS_REVISION)
        _LIKENESS["model"] = WavLMForXVector.from_pretrained(
            VV_LIKENESS_MODEL, revision=VV_LIKENESS_REVISION).eval()
    audio, rate = sf.read(wav, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if rate != 16000:
        audio = librosa.resample(audio, orig_sr=rate, target_sr=16000)
    inputs = _LIKENESS["features"](audio, sampling_rate=16000, return_tensors="pt")
    with torch.no_grad():
        vector = _LIKENESS["model"](**inputs).embeddings[0]
    return torch.nn.functional.normalize(vector, dim=-1).numpy()


def _likeness_margin(wav: str, own_ref: str, other_ref: str) -> float:
    """How much more `wav` sounds like its own character than the other one (cosine of the
    speaker embeddings, own minus other). References are embedded once per render."""
    for ref in (own_ref, other_ref):
        if ref not in _LIKENESS:
            _LIKENESS[ref] = _speaker_embedding(ref)
    take = _speaker_embedding(wav)
    return float(take @ _LIKENESS[own_ref] - take @ _LIKENESS[other_ref])


def synth_vibevoice_gated(text: str, ref_wav: str, other_ref_wav: str, out: str,
                          free_after: bool = False) -> None:
    """synth_vibevoice + two per-line gates: transcription match (said the right words) AND
    likeness (sounds more like this character than the other one, by VV_MARGIN_MIN). Re-roll
    by seed on either failure. free_after threads through to the last line so VibeVoice
    unloads once the reel's final line is synthesized (see synth_vibevoice).

    The model reads the line with its numbers spelled out (spoken_text.spoken). Every
    attempt is recorded in gate.jsonl next to `out`, so a shipped line can be traced to the
    take and the scores that chose it."""
    import shutil
    tts_text = spoken(text)
    gate_log = os.path.join(os.path.dirname(out), "gate.jsonl")
    attempts = []   # (said the words, likeness margin, saved copy)
    for n, seed in enumerate(VV_SEEDS):
        synth_vibevoice(tts_text, ref_wav, out, seed=seed, free_after=free_after)
        ratio, recall, heard = _line_match(out, tts_text)
        margin = _likeness_margin(out, ref_wav, other_ref_wav)
        said_words = ratio >= VV_MATCH_MIN and recall >= VV_RECALL_MIN
        # Preamble-garble guard (2026-10-16): VibeVoice sometimes prepends hallucinated
        # speech before the real line. Whisper's context smoothing hides the extra phrases
        # in the transcript (recall stays 1.0), but the duration betrays it: clean takes run
        # ~0.30-0.39 s/word; the garbled Kroger line_00 ran 0.48. Flag anything slower.
        spd = audio_dur(out) / max(1, len(tts_text.split()))
        pace_ok = spd <= 0.45
        passed = said_words and margin >= VV_MARGIN_MIN and pace_ok
        with open(gate_log, "a") as f:
            f.write(json.dumps({"line": os.path.basename(out), "seed": seed, "text": tts_text,
                                "heard": heard, "ratio": round(ratio, 3), "recall": round(recall, 3),
                                "margin": round(margin, 3), "spd": round(spd, 3), "passed": passed}) + "\n")
        if passed:
            break
        print(f"[gate] re-roll seed={seed} match={ratio:.2f}/{VV_MATCH_MIN} "
              f"recall={recall:.2f}/{VV_RECALL_MIN} margin={margin:+.3f}/{VV_MARGIN_MIN} spd={spd:.2f}/0.45 "
              f"want={tts_text[:45]!r} got={heard[:45]!r}", file=sys.stderr, flush=True)
        saved = f"{out}.take{n}"
        shutil.copyfile(out, saved)
        attempts.append((said_words, margin, saved))
    else:
        # None passed. A take that said the words beats one that did not; among those, the one
        # that sounds most like the character wins. Summing word and likeness scores instead
        # let transcript noise pick a weak-likeness take over clearly better ones (2026-09-17).
        best = max(attempts, key=lambda a: (a[0], a[1]))
        shutil.copyfile(best[2], out)
    for _, _, saved in attempts:
        os.remove(saved)


# --- timeline --------------------------------------------------------------

def build_timeline(lines: list[dict], assets: str, tts: str, workdir: str):
    """Synthesize each line + burps; return (clip_paths, intervals, burps)."""
    clips: list[str] = []
    intervals = {"rick": [], "morty": []}   # [(start,end), ...] per speaker
    burps: list[tuple[float, float]] = []
    line_spans: list[tuple[float, float]] = []   # per line: (start, end incl. the burp after it)
    line_clips: list[tuple[str, float]] = []     # per line: (its wav, where it starts in the reel)
    burp_wav = os.path.join(assets, "burp.wav")
    have_burp = os.path.exists(burp_wav)
    if not have_burp:  # burp:true lines silently degrade — say so (asset was lost
        # once already in the 2026-08 volume re-seed and nobody noticed for days)
        print(f"[burp] {burp_wav} missing — ALL burps skipped this render",
              file=sys.stderr, flush=True)
    if have_burp:  # normalize to 48k mono so it can't desync the concat timeline
        burp_norm = f"{workdir}/burp_48k.wav"
        level_audio(burp_wav, burp_norm, BURP_LUFS)
        burp_wav = burp_norm
    t = 0.0
    for i, ln in enumerate(lines):
        spk = ln.get("speaker", "").lower()
        if spk not in SPEAKERS:
            die(f"line {i}: speaker must be rick|morty", got=spk)
        clip = f"{workdir}/line_{i:02d}.wav"
        if tts == "placeholder":
            synth_placeholder(ln.get("text", ""), spk, clip)
        else:
            ref = os.path.join(assets, f"{spk}.wav")
            other_ref = os.path.join(assets, f"{'morty' if spk == 'rick' else 'rick'}.wav")
            for path in (ref, other_ref):
                if not os.path.exists(path):
                    die("missing voice reference", expected=path)
            # Free VibeVoice-Large after the LAST line so the model doesn't linger in
            # ComfyUI when the GPU lease releases (else llamacpp can't reload -> OOM).
            synth_vibevoice_gated(ln.get("text", ""), ref, other_ref, clip,
                                  free_after=(i == len(lines) - 1))
        raw = f"{workdir}/line_{i:02d}.raw.wav"
        os.replace(clip, raw)
        level_audio(raw, clip, LINE_LUFS)
        d = audio_dur(clip)
        clips.append(clip)
        line_clips.append((clip, round(t, 3)))
        line_start = t
        t += d
        if ln.get("burp") and have_burp:
            clips.append(burp_wav)
            bd = audio_dur(burp_wav)
            burps.append((round(t, 3), round(t + bd, 3)))
            t += bd
        # The speaker stays on screen through their own burp.
        intervals[spk].append((round(line_start, 3), round(t, 3)))
        line_spans.append((round(line_start, 3), round(t, 3)))
    return clips, line_clips, intervals, burps, round(t, 3), line_spans


def concat_audio(clips: list[str], out: str, workdir: str) -> None:
    listf = f"{workdir}/concat.txt"
    with open(listf, "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    joined = f"{workdir}/full.unmastered.wav"
    ff("-f", "concat", "-safe", "0", "-i", listf, "-ar", "48000", "-ac", "1", joined)
    level_audio(joined, out, MASTER_LUFS, MASTER_TRUE_PEAK)


def enable_expr(ivals: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{s},{e})" for s, e in ivals) or "0"


# --- concept cards ---------------------------------------------------------

def card_clip_path(draft: str, index: int) -> str:
    """Where the driver staged concept `index`'s clip. Same name as concept_cards.card_path()
    builds in the agent container: the two agree by this convention, nothing is passed."""
    return f"{draft}/cards/card_{index:02d}.mov"


def concept_cards(concepts: list, line_spans: list[tuple[float, float]], draft: str) -> list[dict]:
    """Resolve declared concepts to [{index, path, start, end, lines}] for the ones whose clip
    exists. Anything unusable is reported on stderr and skipped: a card is an enhancement, the
    reel ships without it."""
    cards: list[dict] = []
    for i, concept in enumerate(concepts or []):
        lines = concept.get("lines") if isinstance(concept, dict) else None
        usable = (isinstance(lines, list) and lines
                  and all(isinstance(x, int) and not isinstance(x, bool) for x in lines)
                  and all(0 <= x < len(line_spans) for x in lines))
        if not usable:
            print(f"[cards] concept {i}: lines {lines!r} invalid for {len(line_spans)} lines; skipped",
                  file=sys.stderr, flush=True)
            continue
        path = card_clip_path(draft, i)
        if not os.path.exists(path):
            print(f"[cards] concept {i}: clip missing at {path}; skipped", file=sys.stderr, flush=True)
            continue
        start = line_spans[min(lines)][0]
        end = line_spans[max(lines)][1]
        if cards and start < cards[-1]["end"]:
            print(f"[cards] concept {i}: window {start}-{end} overlaps the previous card; skipped",
                  file=sys.stderr, flush=True)
            continue
        cards.append({"index": i, "path": path, "start": start, "end": end, "lines": sorted(lines)})
    return cards


# --- compose ---------------------------------------------------------------

def ensure_cutout(png: str) -> str:
    """Return a transparent, bbox-cropped version of `png`, cached beside it.
    Two jobs:
      1. Provided avatars often ship as art on a solid white card (fully-opaque
         alpha) -> composites as an ugly white box. rembg isolates the character.
      2. Sources are padded differently (a small portrait in a big frame vs a
         tight head-shot), so `scale=-1:H` in compose renders them at wildly
         different on-screen sizes. Cropping to the alpha bounding box makes the
         character fill the canvas, so every avatar scales to a consistent size.
    Cache invalidates when the source changes."""
    import numpy as np
    from PIL import Image
    cut = os.path.splitext(png)[0] + "_cut.png"
    if os.path.exists(cut) and os.path.getmtime(cut) >= os.path.getmtime(png):
        return cut
    im = Image.open(png).convert("RGBA")
    if (np.asarray(im)[:, :, 3] < 10).mean() <= 0.02:  # opaque source -> remove bg
        from rembg import new_session, remove
        im = remove(im, session=new_session("u2net"), alpha_matting=True,
                    alpha_matting_foreground_threshold=240,
                    alpha_matting_background_threshold=15)
    a = np.asarray(im)[:, :, 3]
    ys, xs = np.where(a > 10)  # tight-crop to the visible character
    if len(xs):
        im = im.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    im.save(cut)
    return cut


def compose(full_wav: str, dur: float, gameplay: str, assets: str,
            ass: str, intervals: dict, burps: list, story: dict, out: str,
            cards: list[dict] | None = None) -> None:
    rick_png = ensure_cutout(os.path.join(assets, "rick.png"))
    morty_png = ensure_cutout(os.path.join(assets, "morty.png"))
    cards = cards or []

    fc = [
        f"[1:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,setpts=PTS-STARTPTS,eq=saturation=1.12[bg]",
        # The avatar art is a bust that ends in a straight cut at the waist; fading the bottom
        # AVATAR_FADE of its alpha keeps that edge from reading as a floating torso.
        f"[2:v]scale=-1:640,format=rgba,{AVATAR_FADE_GEQ}[rk]",
        f"[3:v]scale=-1:640,format=rgba,{AVATAR_FADE_GEQ}[mo]",
    ]
    # Concept cards: each clip (RGBA, a ~2s animation) is held on its last frame for the rest of
    # its window, alpha-faded in/out, then padded with transparent frames up to its start so the
    # stream runs from t=0 (no timestamp games with overlay). Inputs 4.. are the clips, in order.
    base = "bg"
    for k, card in enumerate(cards):
        win = round(card["end"] - card["start"], 3)
        fade_out_at = max(0.0, win - CARD_FADE_OUT)
        fc.append(
            f"[{4 + k}:v]format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={win:.3f},trim=end={win:.3f},"
            f"fade=t=in:st=0:d={CARD_FADE_IN}:alpha=1,"
            f"fade=t=out:st={fade_out_at:.3f}:d={CARD_FADE_OUT}:alpha=1,"
            f"tpad=start_duration={card['start']:.3f}:start_mode=add:color=black@0.0,"
            f"setpts=PTS-STARTPTS[c{k}]")
        fc.append(
            f"[{base}][c{k}]overlay=x=0:y={CARD_Y}:eof_action=pass:"
            f"enable='between(t,{card['start']},{card['end']})'[k{k}]")
        base = f"k{k}"
    fc += [
        # avatar swapped to the speaker, pinned to its SIDE (Morty left, Rick right)
        # so the centre gameplay stays visible; fully in-frame (30px edge margin, so
        # neither png is cut off).
        f"[{base}][rk]overlay=x=W-w-30:y=H-h-460:enable='{enable_expr(intervals['rick'])}'[a1]",
        f"[a1][mo]overlay=x=30:y=H-h-460:enable='{enable_expr(intervals['morty'])}'[a2]",
        # speaker name tag under the avatar, on its side
        f"[a2]drawtext=text='RICK':font='Noto Sans Black':fontcolor=white:fontsize=54:borderw=6:bordercolor=0x1b5e20:"
        f"x=w-tw-90:y=1440:enable='{enable_expr(intervals['rick'])}'[a2r]",
        f"[a2r]drawtext=text='MORTY':font='Noto Sans Black':fontcolor=black:fontsize=54:borderw=6:bordercolor=0xf9a825:"
        f"x=90:y=1440:enable='{enable_expr(intervals['morty'])}'[a3]",
        # karaoke captions (under the card band — ASS with explicit PlayRes). No chyron.
        f"[a3]ass={ass}[a6]",
    ]
    # The burp SFX stays in the audio track; the on-screen "BURP!" popup was removed at the
    # operator's request (2026-10-20) — the sound alone carries the transition beat.
    last = "a6"

    card_inputs = [arg for card in cards for arg in ("-i", card["path"])]
    ff("-i", full_wav, "-stream_loop", "-1", "-i", gameplay,
       "-i", rick_png, "-i", morty_png, *card_inputs,
       "-filter_complex", ";".join(fc),
       "-map", f"[{last}]", "-map", "0:a", "-t", f"{dur:.3f}",
       "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out)


# --- captions --------------------------------------------------------------

# Hormozi-style caption (matches the original @primus.relay reels): white words
# that fill to a bright highlight as they're spoken, colour rotating per phrase.
# Standard V4+ format WITH SecondaryColour so \kf karaoke works (Primary=filled
# highlight, Secondary=white=unspoken). Top-anchored (Align 8, MarginV 660): under
# the concept-card band (y 90..650), above the avatars' heads (y 820+).
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Noto Sans Black,94,&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,7,3,8,70,70,660,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Hormozi-classic highlight rotation, in ASS &HBBGGRR order (cyan, green, yellow, red).
CAPTION_HIGHLIGHTS = ["&H00FFFF00", "&H0000FF00", "&H0000FFFF", "&H004444FF"]
CAPTION_PHRASE_WORDS = 3


def caption_words(whisper_words) -> list[tuple[float, float, str]]:
    """(start, end, text) per spoken word. Whisper marks a new word with a leading space; a
    token without one continues the previous word ("900" + ",000", "4" + "%"), so it is joined
    onto it instead of becoming a caption word of its own ("900 ,000 WAFERS")."""
    words: list[tuple[float, float, str]] = []
    for w in whisper_words:
        text = w.word.strip()
        if not text:
            continue
        if words and not w.word.startswith(" "):
            start, _, prev = words[-1]
            words[-1] = (start, w.end, prev + text)
        else:
            words.append((w.start, w.end, text))
    return words


def caption_phrases(line_words: list[list[tuple[float, float, str]]]) -> list[list[tuple[float, float, str]]]:
    """Each line's words grouped into short phrases of at most CAPTION_PHRASE_WORDS. A phrase
    never spans two dialogue lines (one speaker's words never share a caption with the other's)
    and ends after a word that ends a sentence."""
    phrases: list[list[tuple[float, float, str]]] = []
    for words in line_words:
        current: list[tuple[float, float, str]] = []
        for word in words:
            if current and (len(current) >= CAPTION_PHRASE_WORDS or current[-1][2][-1] in ".!?"):
                phrases.append(current)
                current = []
            current.append(word)
        if current:
            phrases.append(current)
    return phrases


def transcribe_captions(line_clips: list[tuple[str, float]], ass: str) -> int:
    """Hormozi-style karaoke captions: words grouped into short phrases; each word
    fills white->highlight (\\kf) as it's spoken; highlight colour rotates per phrase.

    Each line is transcribed on its own and shifted to where it starts in the reel. On the
    joined track whisper's word times drift across the burps and line gaps (the next line's
    first word appeared during the burp before it); per line, a word cannot leave its line."""
    from faster_whisper import WhisperModel
    # CPU only, like the per-line gate check (_line_match). This runs AFTER the last VibeVoice
    # prompt has drained from ComfyUI's queue, which is when the GPU admission gate releases
    # the card and the scheduler restores llama.cpp. A CUDA model here would be a second tenant
    # on that card. The endpoint also launches this script with CUDA_VISIBLE_DEVICES="".
    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    line_words = []
    for clip, offset in line_clips:
        segs = transcribe_line(model, clip, word_timestamps=True)
        line_words.append([(offset + a, offset + b, w)
                           for a, b, w in caption_words(w for s in segs for w in (s.words or []))])
    words = [w for line in line_words for w in line]
    if not words:
        die("transcription produced no words", clips=[c for c, _ in line_clips])

    def ts(x: float) -> str:
        h = int(x // 3600); m = int(x // 60 % 60); s = x % 60
        return f"{h:d}:{m:02d}:{s:05.2f}"

    with open(ass, "w") as f:
        f.write(ASS_HEADER)
        for pi, grp in enumerate(caption_phrases(line_words)):
            start, end = grp[0][0], grp[-1][1]
            hl = CAPTION_HIGHLIGHTS[pi % len(CAPTION_HIGHLIGHTS)]
            parts = []
            for idx, (a, b, w) in enumerate(grp):
                nxt = grp[idx + 1][0] if idx + 1 < len(grp) else b  # fill across gaps
                cs = max(1, int(round((nxt - a) * 100)))
                parts.append(f"{{\\kf{cs}}}{w.upper()}")
            text = "{\\1c" + hl + "}" + " ".join(parts)
            f.write(f"Dialogue: 0,{ts(start)},{ts(end)},Karaoke,,0,0,0,,{text}\n")
    return len(words)


def pick_gameplay() -> str:
    # Background gameplay is randomly one of two games (50/50), then a random clip
    # within it. Subway Surfers clips live at the top level (subway_seg_*.mp4);
    # Minecraft parkour clips live in the minecraft/ subdir (mc_seg_*.mp4) — the
    # old top-level *.mp4 glob was non-recursive and only ever saw Subway.
    import glob
    import random
    import sys
    pools = {
        "subway":    sorted(glob.glob(f"{INPUT_GAMEPLAY_DIR}/subway_*.mp4")),
        "minecraft": sorted(glob.glob(f"{INPUT_GAMEPLAY_DIR}/minecraft/*.mp4")),
    }
    available = [g for g, segs in pools.items() if segs]
    if not available:
        die("no gameplay segments", dir=INPUT_GAMEPLAY_DIR)
    game = random.choice(available)  # 50/50 between the two games (not weighted by clip count)
    clip = random.choice(pools[game])
    print(f"[gameplay] {game}: {clip}", file=sys.stderr)
    return clip


def main() -> None:
    global VV_MODEL, VV_STEPS
    p = argparse.ArgumentParser()
    p.add_argument("--dialogue-json", required=True)
    p.add_argument("--assets-dir", default="/root/ComfyUI/input/rickmorty")
    p.add_argument("--tts", choices=["vibevoice", "placeholder"], default="vibevoice")
    p.add_argument("--gameplay", default=None, help="override gameplay clip path")
    p.add_argument("--vv-model", default=VV_MODEL, help="VibeVoice model folder name")
    p.add_argument("--vv-steps", type=int, default=VV_STEPS, help="VibeVoice diffusion steps")
    args = p.parse_args()

    VV_MODEL, VV_STEPS = args.vv_model, args.vv_steps

    try:
        with open(args.dialogue_json) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"cannot read dialogue-json: {e}")

    lines = d.get("lines") or []
    if not lines:
        die("dialogue has no lines")
    slug = d.get("slug") or f"rm_{int(time.time())}"
    story = d.get("story", {})

    draft = f"{OUT_BASE}/draft_{slug}"
    os.makedirs(draft, exist_ok=True)
    t_all = time.time()

    clips, line_clips, intervals, burps, total, line_spans = build_timeline(
        lines, args.assets_dir, args.tts, draft)
    full = f"{draft}/full.wav"
    concat_audio(clips, full, draft)

    caps = f"{draft}/captions.ass"
    n_words = transcribe_captions(line_clips, caps)

    concepts = d.get("concepts") or []
    cards = concept_cards(concepts, line_spans, draft)

    gameplay = args.gameplay or pick_gameplay()
    reel = f"{draft}/reel.mp4"
    compose(full, total, gameplay, args.assets_dir, caps, intervals, burps, story, reel, cards)

    meta = {
        "slug": slug, "tts": args.tts, "lines": len(lines), "burps": len(burps),
        "duration_sec": total, "word_count": n_words,
        "reel_size_bytes": os.path.getsize(reel),
        "burp_windows": burps,
        "line_spans": line_spans,
        "concept_cards": {"declared": len(concepts), "shown": cards},
        "elapsed_sec": round(time.time() - t_all, 2),
    }
    with open(f"{draft}/meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps({"ok": True, "draft_dir": draft, "reel_path": reel, **meta}), flush=True)


if __name__ == "__main__":
    main()
