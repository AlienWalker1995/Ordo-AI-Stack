"""Pick between original audio/timings and VibeVoice-cloned audio/timings.

Plug this between the VibeVoice subgraph and downstream consumers (caption
render + audio concat). With `bypass=True`, downstream gets the original
audio and its first-pass word_timings — VibeVoice + its re-transcribe step
become irrelevant to the final mux. With `bypass=False`, it forwards the
cloned voice + the second-pass timings.

The `new_*` inputs are optional, so if the VibeVoice subgraph is muted
(Ctrl+M) the node falls back to original automatically instead of erroring.
"""


class OrdoBypassToggle:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "bypass": ("BOOLEAN", {"default": False,
                                       "tooltip": "True = keep original audio + timings, skip VibeVoice."}),
                "orig_audio": ("AUDIO",),
                "orig_timings": ("ORDO_WORD_TIMINGS",),
            },
            "optional": {
                "new_audio": ("AUDIO",),
                "new_timings": ("ORDO_WORD_TIMINGS",),
            },
        }

    RETURN_TYPES = ("AUDIO", "ORDO_WORD_TIMINGS")
    RETURN_NAMES = ("audio", "word_timings")
    FUNCTION = "select"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Switch between original and VibeVoice-cloned audio/timings."

    def select(self, bypass, orig_audio, orig_timings, new_audio=None, new_timings=None):
        if bypass or new_audio is None:
            return (orig_audio, orig_timings)
        out_timings = new_timings if new_timings is not None else orig_timings
        return (new_audio, out_timings)
