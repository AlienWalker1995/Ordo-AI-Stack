"""services/voice: the TTS image answers response_format="pcm".

Hermes's streaming speech (desktop speak-stream, Discord voice) asks the OpenAI-compatible endpoint
for raw PCM one sentence at a time. Chatterbox-TTS-Server at the pinned commit accepts only wav,
opus and mp3, so every streamed reply was refused with a 422 and the reply was spoken only after it
had finished. The image applies patches/openai-pcm.patch to add the format; these checks keep the
patch in the build and keep it doing what Hermes needs.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE = ROOT / "services" / "voice"
PATCH = VOICE / "patches" / "openai-pcm.patch"


def test_the_dockerfile_applies_the_pcm_patch_before_dropping_git():
    dockerfile = (VOICE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY patches/ /tmp/patches/" in dockerfile
    apply_at = dockerfile.index("git -C /app apply --verbose /tmp/patches/openai-pcm.patch")
    assert apply_at < dockerfile.index("rm -rf /app/.git")


def test_the_patch_accepts_pcm_and_reports_its_rate():
    added = "\n".join(line[1:] for line in PATCH.read_text(encoding="utf-8").splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    assert re.search(r'response_format: Literal\[[^\]]*"pcm"', added)
    assert 'output_format == "pcm"' in added
    assert '"X-Audio-Sample-Rate"' in added      # the header Hermes reads the rate from
    assert "target_sample_rate=None if is_pcm" in added  # never resampled to a rate the client cannot see
