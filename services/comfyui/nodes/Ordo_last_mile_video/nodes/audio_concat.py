"""Concatenate two ComfyUI AUDIO dicts along the time axis.

ComfyUI AUDIO format: {"waveform": Tensor[B, C, T], "sample_rate": int}.
The second clip is resampled to the first clip's sample rate if needed, and
its channel count is broadcast to match (mono->stereo via repeat, multi->mono
via mean) so torch.cat works cleanly.
"""

import torch


def _match_channels(wave, target_channels):
    cur = wave.shape[1]
    if cur == target_channels:
        return wave
    if cur == 1 and target_channels > 1:
        return wave.repeat(1, target_channels, 1)
    if cur > 1 and target_channels == 1:
        return wave.mean(dim=1, keepdim=True)
    if cur < target_channels:
        pad = wave[:, :1, :].repeat(1, target_channels - cur, 1)
        return torch.cat([wave, pad], dim=1)
    return wave[:, :target_channels, :]


class OrdoAudioConcat:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio_a": ("AUDIO",),
                "audio_b": ("AUDIO",),
            },
            "optional": {
                "silence_gap_s": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "bypass": ("BOOLEAN", {"default": False,
                                       "tooltip": "True = skip the concat and return audio_a unchanged (no endcard audio appended)."}),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "concat"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Concatenate audio_a + (optional silence) + audio_b. Matches sample rate and channels to audio_a."

    def concat(self, audio_a, audio_b, silence_gap_s=0.0, bypass=False):
        if bypass:
            return (audio_a,)
        sr = int(audio_a["sample_rate"])
        wave_a = audio_a["waveform"]
        wave_b = audio_b["waveform"]

        if int(audio_b["sample_rate"]) != sr:
            import torchaudio.functional as F
            wave_b = F.resample(wave_b, int(audio_b["sample_rate"]), sr)

        target_channels = wave_a.shape[1]
        wave_b = _match_channels(wave_b, target_channels)

        # Pad batch dim if mismatched (take batch 0 of each as the canonical track).
        if wave_a.shape[0] != wave_b.shape[0]:
            wave_a = wave_a[:1]
            wave_b = wave_b[:1]

        parts = [wave_a]
        if silence_gap_s > 0:
            gap_samples = int(round(silence_gap_s * sr))
            silence = torch.zeros(wave_a.shape[0], target_channels, gap_samples,
                                  dtype=wave_a.dtype, device=wave_a.device)
            parts.append(silence)
        parts.append(wave_b.to(wave_a.dtype).to(wave_a.device))

        out = torch.cat(parts, dim=-1)
        return ({"waveform": out, "sample_rate": sr},)
