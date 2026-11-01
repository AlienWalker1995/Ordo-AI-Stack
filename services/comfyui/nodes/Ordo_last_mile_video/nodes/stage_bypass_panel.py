"""Single control panel for last-mile stage bypass toggles.

Five BOOLEAN widgets, five BOOLEAN outputs. Each output wires to the matching
`bypass` input on a stage node — when wired, that stage node's per-node bypass
widget hides automatically, so all toggles live on this one panel instead of
scattered across the canvas.
"""


class OrdoStageBypassPanel:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "captions": ("BOOLEAN", {"default": False,
                                         "tooltip": "Skip caption burn-in (OrdoRenderCaptions)."}),
                "logo": ("BOOLEAN", {"default": False,
                                     "tooltip": "Skip bouncing-logo overlay (OrdoDVDLogoOverlay)."}),
                "llm_refine": ("BOOLEAN", {"default": False,
                                           "tooltip": "Skip LLM transcript polish (OrdoLLMRefine)."}),
                "fixups": ("BOOLEAN", {"default": False,
                                       "tooltip": "Skip literal/regex replacements (OrdoCaptionFixups)."}),
                "endcard_audio": ("BOOLEAN", {"default": False,
                                              "tooltip": "Skip endcard audio concat (OrdoAudioConcat)."}),
            },
        }

    RETURN_TYPES = ("BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN", "BOOLEAN")
    RETURN_NAMES = ("captions", "logo", "llm_refine", "fixups", "endcard_audio")
    FUNCTION = "panel"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "One-stop bypass toggles for each stage of the last-mile workflow."

    def panel(self, captions, logo, llm_refine, fixups, endcard_audio):
        return (captions, logo, llm_refine, fixups, endcard_audio)
