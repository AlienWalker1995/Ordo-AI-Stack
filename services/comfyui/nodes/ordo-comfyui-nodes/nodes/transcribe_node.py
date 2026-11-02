"""Ordo Transcribe Audio — speech-to-text via faster_whisper with word timestamps."""

import logging
import os
import tempfile

import numpy as np
import torch

logger = logging.getLogger("Ordo")


class OrdoTranscribeAudioNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO", {"tooltip": "Audio to transcribe (ComfyUI AUDIO dict)"}),
                "model_size": (["tiny", "base", "small", "medium", "large-v2", "large-v3"], {
                    "default": "base",
                    "tooltip": "Whisper model size. Larger = more accurate but slower."
                }),
                "device": (["cuda", "cpu"], {
                    "default": "cuda",
                    "tooltip": "Device for transcription inference"
                }),
            },
            "optional": {
                "language": ("STRING", {
                    "default": "en",
                    "tooltip": "Language code (e.g. en, es). Pin to avoid Whisper guessing on long silences."
                }),
                "initial_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": "Vocabulary/context hint to bias transcription. e.g. proper nouns, jargon."
                }),
                "anti_hallucinate": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Strict mode: temperature=0, condition_on_previous_text=False, VAD filter on. Recommended."
                }),
                "beam_size": ("INT", {
                    "default": 5, "min": 1, "max": 10,
                    "tooltip": "Beam search width. Higher = more accurate, slower."
                }),
            },
        }

    RETURN_TYPES = ("ORDO_WORD_TIMINGS",)
    RETURN_NAMES = ("word_timings",)
    FUNCTION = "transcribe"
    CATEGORY = "Ordo"
    DESCRIPTION = "Transcribe audio to word-level timestamps using faster-whisper"

    def transcribe(self, audio, model_size="base", device="cuda",
                   language="en", initial_prompt="", anti_hallucinate=True, beam_size=5):
        from faster_whisper import WhisperModel

        # Extract waveform from ComfyUI AUDIO dict
        waveform = audio["waveform"]  # shape: [batch, channels, samples]
        sample_rate = audio["sample_rate"]

        # Convert to mono float32 numpy at 16kHz for whisper
        if isinstance(waveform, torch.Tensor):
            wav_np = waveform.squeeze(0).mean(dim=0).cpu().numpy()  # mono
        else:
            wav_np = np.array(waveform).squeeze()
            if wav_np.ndim > 1:
                wav_np = wav_np.mean(axis=0)

        # Resample to 16kHz if needed
        if sample_rate != 16000:
            import torchaudio.functional as F
            wav_tensor = torch.from_numpy(wav_np).unsqueeze(0).float()
            wav_tensor = F.resample(wav_tensor, sample_rate, 16000)
            wav_np = wav_tensor.squeeze(0).numpy()

        # Save to temp WAV for faster-whisper
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            import soundfile as sf
            sf.write(wav_path, wav_np, 16000, subtype='PCM_16')

            compute_type = "float16" if device == "cuda" else "int8"
            model = WhisperModel(model_size, device=device, compute_type=compute_type)

            # Anti-hallucination knobs:
            # - condition_on_previous_text=False stops Whisper from inventing
            #   continuations of prior segments (the main source of "I was the bloog"
            #   style errors on long monologue audio).
            # - temperature=0 forces deterministic decoding (no creative fallbacks).
            # - VAD filter drops silent regions so Whisper has nothing to hallucinate over.
            transcribe_kwargs = dict(
                word_timestamps=True,
                language=(language.strip() or None),
                beam_size=beam_size,
                initial_prompt=(initial_prompt.strip() or None),
            )
            if anti_hallucinate:
                transcribe_kwargs.update(
                    temperature=0.0,
                    condition_on_previous_text=False,
                    compression_ratio_threshold=2.4,
                    no_speech_threshold=0.5,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )

            segments, _info = model.transcribe(wav_path, **transcribe_kwargs)

            word_timings = []
            for seg in segments:
                for w in (seg.words or []):
                    text = w.word.strip()
                    if text:
                        word_timings.append({
                            "word": text,
                            "start": w.start,
                            "end": w.end,
                            "segment_idx": -1,
                        })

            logger.info(
                f"Transcribed {len(word_timings)} words using {model_size} "
                f"(lang={language!r} anti_hallucinate={anti_hallucinate})"
            )
        finally:
            os.unlink(wav_path)

        return (word_timings,)
