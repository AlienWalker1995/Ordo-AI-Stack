"""Join ORDO_WORD_TIMINGS into a single STRING so it can drive VibeVoice."""


class OrdoWordTimingsToText:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "word_timings": ("ORDO_WORD_TIMINGS",),
            },
            "optional": {
                "separator": ("STRING", {"default": " ", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "convert"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Concatenate word_timings into a single plain-text string for TTS input."

    def convert(self, word_timings, separator=" "):
        words = [str(w.get("word", "")).strip() for w in (word_timings or [])]
        text = separator.join(w for w in words if w)
        return (text,)
