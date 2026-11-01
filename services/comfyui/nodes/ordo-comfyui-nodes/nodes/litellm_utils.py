"""
Shared utilities for Ordo AI nodes that communicate with a LiteLLM proxy.

Provides tensor ↔ base64 conversion, multimodal content building,
and the core API call to a LiteLLM /v1/chat/completions endpoint.
"""

import base64
import io
import logging
import random

import numpy as np
import requests
import torch
from PIL import Image

logger = logging.getLogger("Ordo")


# ── Tensor / image conversion ────────────────────────────────────────────

def tensor_to_base64_png(tensor: torch.Tensor) -> str:
    """Convert a single ComfyUI IMAGE tensor [H,W,C] float32 0-1 → base64 PNG string."""
    arr = tensor.cpu().numpy()
    arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def base64_to_tensor(b64: str) -> torch.Tensor:
    """Convert a base64 image string → ComfyUI IMAGE tensor [1,H,W,3] float32 0-1."""
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)  # [1, H, W, 3]


# ── Multimodal content helpers ────────────────────────────────────────────

def build_image_content_parts(prompt: str, images: list[torch.Tensor]) -> list[dict]:
    """Build OpenAI-format multimodal content array (images first, then text).

    Args:
        prompt: Text prompt.
        images: List of ComfyUI IMAGE tensors (each [H,W,C] — first frame only).

    Returns:
        List of content part dicts for the messages API.
    """
    parts = []
    for img_tensor in images:
        b64 = tensor_to_base64_png(img_tensor)
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    parts.append({"type": "text", "text": prompt})
    return parts


# ── Response parsing ─────────────────────────────────────────────────────

def _parse_image_response(data: dict) -> tuple[str | None, str]:
    """Parse a chat completion response for generated image data and text.

    Handles four response shapes:
      1. LiteLLM images array — message.images[] with image_url.url data URIs
      2. List-of-parts (multimodal) — content is a list with image_url / text parts
      3. Data URI in string — content is a string containing data:image/...;base64,...
      4. inline_data (native Gemini format) — parts with inline_data.data

    Returns:
        (base64_image_data or None, response_text)
    """
    choices = data.get("choices", [])
    if not choices:
        return None, ""

    message = choices[0].get("message", {})
    content = message.get("content")
    response_text = ""
    image_b64 = None

    # Shape 1: LiteLLM returns images in a separate "images" array
    images = message.get("images", [])
    if images:
        for img in images:
            if isinstance(img, dict):
                url = img.get("image_url", {}).get("url", "")
                if url.startswith("data:") and "," in url:
                    image_b64 = url.split(",", 1)[-1]
                    break
        # Text may still be in content
        if isinstance(content, str) and content:
            response_text = content
        return image_b64, response_text

    # Shape 2: content is a list of parts (OpenAI multimodal)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    response_text += part.get("text", "")
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:") and "," in url:
                        image_b64 = url.split(",", 1)[-1]
                elif "inline_data" in part:
                    image_b64 = part["inline_data"].get("data")
        return image_b64, response_text

    # Shape 3 & 4: content is a string
    if isinstance(content, str):
        if "data:image/" in content:
            idx = content.index("data:image/")
            before_text = content[:idx].strip()
            data_uri = content[idx:]
            image_b64 = data_uri.split(",", 1)[-1].split()[0] if "," in data_uri else None
            response_text = before_text
        else:
            response_text = content

    return image_b64, response_text


# ── Core API call ─────────────────────────────────────────────────────────

SAFETY_CATEGORIES = [
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
]


def call_litellm_image_generation(
    proxy_url: str,
    api_key: str,
    model: str,
    content,  # str or list of content parts
    *,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    output_format: str = "PNG",
    temperature: float = 0.8,
    top_p: float = 0.95,
    top_k: int = 40,
    system_instruction: str = "",
    safety_settings: list[dict] | None = None,
    seed: int = -1,
) -> tuple[torch.Tensor | None, str]:
    """Call LiteLLM proxy for image generation and return (image_tensor, response_text).

    Args:
        proxy_url: Base URL of the LiteLLM proxy (e.g. http://localhost:4000).
        api_key: Optional API key / virtual key for the proxy.
        model: Model alias registered in LiteLLM (e.g. "nano-banana-2").
        content: Either a text string or a list of multimodal content parts.
        aspect_ratio: Desired aspect ratio string.
        image_size: "1K", "2K", or "4K".
        output_format: "PNG" or "JPEG".
        temperature: Sampling temperature.
        top_p: Top-p sampling.
        top_k: Top-k sampling.
        system_instruction: Optional system message.
        safety_settings: List of safety setting dicts.
        seed: Random seed (-1 for random).

    Returns:
        (image_tensor [1,H,W,3] or None, response_text)
    """
    url = f"{proxy_url.rstrip('/')}/v1/chat/completions"

    # Build messages
    messages = []
    if system_instruction.strip():
        messages.append({"role": "system", "content": system_instruction.strip()})

    if isinstance(content, str):
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": content})

    # Resolve seed
    actual_seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)

    # Determine output mime type
    mime_type = "image/png" if output_format.upper() == "PNG" else "image/jpeg"

    # Build request payload
    # Gemini-specific params go at the top level so LiteLLM proxy forwards
    # them as kwargs to litellm.completion(), which maps them into the
    # native Gemini generationConfig.
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "seed": actual_seed,
        "response_modalities": ["TEXT", "IMAGE"],
        "image_generation_config": {
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
            "output_mime_type": mime_type,
        },
    }

    if safety_settings:
        payload["safety_settings"] = safety_settings

    # Build headers
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Make the request
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
    except requests.ConnectionError:
        raise RuntimeError(
            f"Cannot connect to LiteLLM proxy at {proxy_url}. "
            "Check that the proxy is running and the URL is correct."
        )
    except requests.Timeout:
        raise RuntimeError(
            "Request to LiteLLM proxy timed out after 120s. "
            "The model may be overloaded — try again later."
        )

    if resp.status_code == 401:
        raise RuntimeError(
            "Authentication failed (401). Check your proxy API key."
        )
    if resp.status_code == 429:
        raise RuntimeError(
            "Rate limited (429). Too many requests — try again later."
        )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(
            f"LiteLLM proxy returned HTTP {resp.status_code}: {detail}"
        )

    data = resp.json()
    image_b64, response_text = _parse_image_response(data)

    if image_b64 is None:
        raise RuntimeError(
            "No image was returned by the model. "
            "The prompt may have been blocked by safety filters, or the model "
            "did not produce an image. Try a different prompt."
        )

    image_tensor = base64_to_tensor(image_b64)
    return image_tensor, response_text
