"""
OrdoNanoBanana2Node — text-to-image generation via LiteLLM proxy (Gemini Flash Image).
"""

import logging

from .litellm_utils import SAFETY_CATEGORIES, call_litellm_image_generation

logger = logging.getLogger("Ordo")

ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
IMAGE_SIZES = ["1K", "2K", "4K"]
OUTPUT_FORMATS = ["PNG", "JPEG"]
SAFETY_THRESHOLDS = [
    "BLOCK_NONE",
    "BLOCK_LOW_AND_ABOVE",
    "BLOCK_MEDIUM_AND_ABOVE",
    "BLOCK_ONLY_HIGH",
]


class OrdoNanoBanana2Node:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "proxy_config": ("ORDO_PROXY_CONFIG",),
                "prompt": ("STRING", {
                    "multiline": True,
                    "tooltip": "Text prompt describing the image to generate",
                }),
                "model": ("STRING", {
                    "default": "nano-banana-2",
                    "tooltip": "LiteLLM model alias",
                }),
                "aspect_ratio": (ASPECT_RATIOS, {"default": "1:1"}),
                "image_size": (IMAGE_SIZES, {"default": "1K"}),
                "temperature": ("FLOAT", {
                    "default": 0.8, "min": 0.0, "max": 1.0, "step": 0.05,
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.05,
                }),
                "top_k": ("INT", {
                    "default": 40, "min": 1, "max": 64,
                }),
                "output_format": (OUTPUT_FORMATS, {"default": "PNG"}),
                "system_instruction": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Optional system instruction for the model",
                }),
                "safety_harassment": (SAFETY_THRESHOLDS, {
                    "default": "BLOCK_MEDIUM_AND_ABOVE",
                }),
                "safety_hate_speech": (SAFETY_THRESHOLDS, {
                    "default": "BLOCK_MEDIUM_AND_ABOVE",
                }),
                "safety_sexually_explicit": (SAFETY_THRESHOLDS, {
                    "default": "BLOCK_MEDIUM_AND_ABOVE",
                }),
                "safety_dangerous_content": (SAFETY_THRESHOLDS, {
                    "default": "BLOCK_MEDIUM_AND_ABOVE",
                }),
                "seed": ("INT", {
                    "default": -1, "min": -1, "max": 2**31 - 1,
                    "tooltip": "-1 for random seed",
                }),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "response_text")
    FUNCTION = "generate"
    CATEGORY = "Ordo/AI"
    DESCRIPTION = "Generate an image from a text prompt using Nano Banana 2 (Gemini Flash Image) via LiteLLM proxy"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def generate(
        self,
        proxy_config: dict,
        prompt: str,
        model: str,
        aspect_ratio: str,
        image_size: str,
        temperature: float,
        top_p: float,
        top_k: int,
        output_format: str,
        system_instruction: str,
        safety_harassment: str,
        safety_hate_speech: str,
        safety_sexually_explicit: str,
        safety_dangerous_content: str,
        seed: int,
    ):
        safety_settings = [
            {"category": cat, "threshold": thresh}
            for cat, thresh in zip(SAFETY_CATEGORIES, [
                safety_harassment,
                safety_hate_speech,
                safety_sexually_explicit,
                safety_dangerous_content,
            ])
        ]

        image_tensor, response_text = call_litellm_image_generation(
            proxy_url=proxy_config["url"],
            api_key=proxy_config["api_key"],
            model=model,
            content=prompt,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            output_format=output_format,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            system_instruction=system_instruction,
            safety_settings=safety_settings,
            seed=seed,
        )

        return (image_tensor, response_text)
