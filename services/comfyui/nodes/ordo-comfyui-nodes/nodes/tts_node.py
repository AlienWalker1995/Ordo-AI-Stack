"""Ordo TTS (Edge) — text-to-speech via edge_tts with word-level timestamps."""

import asyncio
import io
import logging

logger = logging.getLogger("Ordo")

# Common edge_tts voices
VOICE_LIST = [
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-US-AriaNeural",
    "en-US-DavisNeural",
    "en-US-AmberNeural",
    "en-US-AndrewNeural",
    "en-US-BrandonNeural",
    "en-US-ChristopherNeural",
    "en-US-CoraNeural",
    "en-US-ElizabethNeural",
    "en-US-EricNeural",
    "en-US-JacobNeural",
    "en-US-MichelleNeural",
    "en-US-MonicaNeural",
    "en-US-SaraNeural",
    "en-US-SteffanNeural",
    "en-GB-RyanNeural",
    "en-GB-SoniaNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamNeural",
]


class OrdoTTSEdgeNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {
                    "multiline": True,
                    "default": "Hello, this is a test of the Ordo text-to-speech system.",
                    "tooltip": "Text to convert to speech"
                }),
                "voice": (VOICE_LIST, {
                    "default": "en-US-GuyNeural",
                    "tooltip": "Edge TTS voice name"
                }),
            }
        }

    RETURN_TYPES = ("AUDIO", "ORDO_WORD_TIMINGS")
    RETURN_NAMES = ("audio", "word_timings")
    FUNCTION = "generate"
    CATEGORY = "Ordo"
    DESCRIPTION = "Generate speech from text using Microsoft Edge TTS with word-level timestamps"

    def generate(self, text: str, voice: str = "en-US-GuyNeural"):
        # Run async edge_tts in a new event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an existing loop (e.g. ComfyUI's), use a new thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    audio_bytes, word_timings = pool.submit(
                        asyncio.run, self._generate_async(text, voice)
                    ).result()
            else:
                audio_bytes, word_timings = loop.run_until_complete(
                    self._generate_async(text, voice)
                )
        except RuntimeError:
            audio_bytes, word_timings = asyncio.run(
                self._generate_async(text, voice)
            )

        # Decode mp3 bytes to waveform using torchaudio
        import torchaudio

        buffer = io.BytesIO(audio_bytes)
        waveform, sample_rate = torchaudio.load(buffer, format="mp3")

        # ComfyUI AUDIO format: {"waveform": [batch, channels, samples], "sample_rate": int}
        audio_dict = {
            "waveform": waveform.unsqueeze(0),  # add batch dim
            "sample_rate": sample_rate,
        }

        logger.info(f"Generated TTS: {len(word_timings)} words, {waveform.shape[-1]/sample_rate:.1f}s")

        return (audio_dict, word_timings)

    async def _generate_async(self, text: str, voice: str):
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        word_timings = []
        audio_chunks = []

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                offset_ms = chunk["offset"] / 10_000  # 100-ns units -> ms
                duration_ms = chunk["duration"] / 10_000
                start_s = offset_ms / 1000.0
                end_s = start_s + duration_ms / 1000.0
                w = chunk["text"].strip()
                if w:
                    word_timings.append({
                        "word": w,
                        "start": start_s,
                        "end": end_s,
                        "segment_idx": -1,
                    })

        audio_bytes = b"".join(audio_chunks)
        return audio_bytes, word_timings
