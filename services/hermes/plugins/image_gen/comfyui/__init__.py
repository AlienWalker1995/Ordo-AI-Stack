"""Image generation on the stack's own ComfyUI: MiniMax-H3-Image, text to image and editing.

The graph is the repo-owned workflow ComfyUI seeds at boot (scripts/comfyui/workflows/
minimax-h3-image.json, served as user data at workflows/ordo/minimax-h3-image.json), so this
provider and a human in the ComfyUI UI run the same file. It fills in the prompt, size and seed,
adds a LoadImage node per source picture for an edit, and submits through the GPU admission gate
(COMFYUI_URL), which takes GPU residency before the render starts. Nothing here talks to
comfyui:8188 directly, and nothing here touches the GPU lease itself.

Selected with ``image_gen.provider: comfyui`` (the stack's boot hook sets it once).
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)
from plugins.image_gen._common import collect_source_images, error_factory

logger = logging.getLogger(__name__)

PROVIDER = "comfyui"
MODEL_ID = "minimax-h3-image"
WORKFLOW_USERDATA_PATH = "workflows/ordo/minimax-h3-image.json"

# Node ids in that workflow. A change there must be matched here; the stack's tests hold the two
# together.
PROMPT_NODE = "4"       # MiniMaxH3ImageConditioning: prompt, width, height, images.image_<n>
SAMPLER_NODE = "6"      # KSampler: seed
SAVE_NODE = "8"         # SaveImage

# About 4 MP at each of Hermes' three aspects, in multiples of 32 (the model's canvas step). The
# model is tuned for 2048x2048 of area. An edit keeps its picture's size.
SIZES = {"square": (2048, 2048), "landscape": (2720, 1536), "portrait": (1536, 2720)}

# The model reads up to 16 pictures: the one to edit plus 15 references.
MAX_SOURCE_IMAGES = 16
MAX_SOURCE_BYTES = 25 * 1024 * 1024

# A cold render loads about 37 GB of weights and may first wait for the GPU lease (the chat model
# is moved off the card), so the ceiling is generous. Configurable as image_gen.comfyui.timeout_s.
DEFAULT_TIMEOUT_S = 1200
POLL_INTERVAL_S = 3.0
HTTP_TIMEOUT_S = 60


def _gate_url() -> str:
    return os.environ.get("COMFYUI_URL", "").strip().rstrip("/")


def _timeout_s() -> float:
    try:
        from plugins.image_gen._common import load_image_gen_config
        section = load_image_gen_config("comfyui")
        return float(section.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def _source_bytes(source: str) -> tuple[bytes, str]:
    """The bytes and a file name of one source picture: a data URI, an http(s) URL or a local path."""
    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        mime = header[5:].split(";")[0] or "image/png"
        data = base64.b64decode(payload, validate=True)
        return data, "source" + (mimetypes.guess_extension(mime) or ".png")
    if source.startswith(("http://", "https://")):
        response = requests.get(source, timeout=HTTP_TIMEOUT_S, stream=True)
        response.raise_for_status()
        data = response.raw.read(MAX_SOURCE_BYTES + 1, decode_content=True)
        name = Path(source.split("?")[0]).name or "source.png"
        return data, name
    path = Path(source).expanduser()
    return path.read_bytes(), path.name


class ComfyUIImageGenProvider(ImageGenProvider):
    """MiniMax-H3-Image on the stack's ComfyUI, through the GPU admission gate."""

    @property
    def name(self) -> str:
        return PROVIDER

    @property
    def display_name(self) -> str:
        return "ComfyUI (local MiniMax-H3-Image)"

    def is_available(self) -> bool:
        return bool(_gate_url())

    def list_models(self) -> list[dict[str, Any]]:
        return [{
            "id": MODEL_ID, "display": "MiniMax-H3-Image",
            "speed": "about 1-2 min warm, longer when the GPU must first be freed",
            "strengths": "Local, no API key. Text to image and editing of up to 16 pictures, "
                         "named <Picture 1>, <Picture 2>, ... in the prompt. Wants a detailed "
                         "English description of the whole image.",
        }]

    def default_model(self) -> str | None:
        return MODEL_ID

    def get_setup_schema(self) -> dict[str, Any]:
        return {"name": self.display_name, "badge": "local",
                "tag": "MiniMax-H3-Image on this stack's ComfyUI; needs COMFYUI_URL (the GPU gate)",
                "env_vars": []}

    def capabilities(self) -> dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": MAX_SOURCE_IMAGES - 1}

    def generate(self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *,
                 image_url: str | None = None, reference_image_urls: list[str] | None = None,
                 **kwargs: Any) -> dict[str, Any]:
        aspect = resolve_aspect_ratio(aspect_ratio)
        fail = error_factory(PROVIDER, aspect, model=MODEL_ID, prompt=prompt)
        if not isinstance(prompt, str) or not prompt.strip():
            return fail("Prompt is required and must be a non-empty string", "invalid_prompt")
        base = _gate_url()
        if not base:
            return fail("COMFYUI_URL is not set (it must point at the ComfyUI GPU gate)", "not_configured")

        sources = (collect_source_images(image_url, reference_image_urls) or [])[:MAX_SOURCE_IMAGES]
        try:
            graph = self._workflow(base)
            uploaded = [self._upload(base, source) for source in sources]
        except _Failure as exc:
            return fail(str(exc), exc.error_type)

        conditioning = graph[PROMPT_NODE]["inputs"]
        conditioning["prompt"] = prompt.strip()
        if uploaded:
            # An edit keeps the size of the picture it edits (0 = image_1's size, about 4 MP).
            conditioning["width"], conditioning["height"] = 0, 0
        else:
            conditioning["width"], conditioning["height"] = SIZES[aspect]
        for index, name in enumerate(uploaded, start=1):
            node_id = f"load_{index}"
            graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
            conditioning[f"images.image_{index}"] = [node_id, 0]
        seed = kwargs.get("seed")
        graph[SAMPLER_NODE]["inputs"]["seed"] = int(seed) if isinstance(seed, int) else random.randrange(2**48)

        try:
            image_bytes = self._run(base, graph)
        except _Failure as exc:
            return fail(str(exc), exc.error_type)
        path = save_b64_image(base64.b64encode(image_bytes).decode("ascii"), prefix="comfyui_h3", extension="png")
        return success_response(
            image=str(path), model=MODEL_ID, prompt=prompt, aspect_ratio=aspect, provider=PROVIDER,
            modality="image" if uploaded else "text",
            extra={"seed": graph[SAMPLER_NODE]["inputs"]["seed"],
                   "width": conditioning["width"], "height": conditioning["height"]})

    # --- ComfyUI calls ------------------------------------------------------------------------

    @staticmethod
    def _workflow(base: str) -> dict[str, Any]:
        url = f"{base}/api/userdata/{quote(WORKFLOW_USERDATA_PATH, safe='')}"
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as exc:
            raise _Failure(f"ComfyUI is not answering at {base}: {exc}", "unreachable") from exc
        if response.status_code == 404:
            raise _Failure(f"ComfyUI has no {WORKFLOW_USERDATA_PATH}; it is seeded at ComfyUI boot",
                           "not_configured")
        if not response.ok:
            raise _Failure(f"Reading the workflow failed: HTTP {response.status_code}", "api_error")
        graph = {k: v for k, v in response.json().items() if not k.startswith("_")}
        for node in (PROMPT_NODE, SAMPLER_NODE, SAVE_NODE):
            if node not in graph:
                raise _Failure(f"The workflow has no node {node}; it no longer matches this provider",
                               "not_configured")
        return graph

    @staticmethod
    def _upload(base: str, source: str) -> str:
        try:
            data, filename = _source_bytes(source)
        except (OSError, ValueError, binascii.Error, requests.RequestException) as exc:
            raise _Failure(f"Could not read source image {source[:80]}: {exc}", "io_error") from exc
        if len(data) > MAX_SOURCE_BYTES:
            raise _Failure(f"Source image {filename} is over {MAX_SOURCE_BYTES >> 20} MB", "io_error")
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        unique = f"hermes_{uuid.uuid4().hex[:12]}_{filename}"
        try:
            response = requests.post(f"{base}/upload/image", files={"image": (unique, data, mime)},
                                     data={"type": "input", "overwrite": "true"}, timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as exc:
            raise _Failure(f"Uploading {filename} failed: {exc}", "unreachable") from exc
        if not response.ok:
            raise _Failure(f"Uploading {filename} failed: HTTP {response.status_code}", "api_error")
        body = response.json()
        subfolder = body.get("subfolder") or ""
        return f"{subfolder}/{body['name']}" if subfolder else body["name"]

    @staticmethod
    def _run(base: str, graph: dict[str, Any]) -> bytes:
        """Submit through the gate, wait for the result, return the saved PNG's bytes."""
        try:
            response = requests.post(f"{base}/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex},
                                     timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as exc:
            raise _Failure(f"Submitting to ComfyUI failed: {exc}", "unreachable") from exc
        if not response.ok:
            # The gate answers 503 when the GPU cannot be granted; ComfyUI answers 400 with
            # node_errors for a graph it rejects. Either way the body says why.
            raise _Failure(f"ComfyUI refused the render: HTTP {response.status_code} {response.text[:500]}",
                           "api_error")
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise _Failure("ComfyUI accepted the render but returned no prompt_id", "invalid_response")

        deadline = time.monotonic() + _timeout_s()
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            try:
                history = requests.get(f"{base}/history/{prompt_id}", timeout=HTTP_TIMEOUT_S).json()
            except (requests.RequestException, ValueError) as exc:
                logger.debug("history poll for %s failed: %s", prompt_id, exc)
                continue
            entry = history.get(prompt_id)
            if not entry:
                continue
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                messages = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                detail = messages[-1][1].get("exception_message", "") if messages else ""
                raise _Failure(f"The render failed in ComfyUI: {detail.strip()[:500]}", "api_error")
            images = (entry.get("outputs", {}).get(SAVE_NODE) or {}).get("images") or []
            if images:
                image = images[0]
                try:
                    view = requests.get(f"{base}/view", timeout=HTTP_TIMEOUT_S, params={
                        "filename": image["filename"], "subfolder": image.get("subfolder", ""),
                        "type": image.get("type", "output")})
                except requests.RequestException as exc:
                    raise _Failure(f"Fetching the result failed: {exc}", "unreachable") from exc
                if not view.ok:
                    raise _Failure(f"Fetching the result failed: HTTP {view.status_code}", "api_error")
                return view.content
            if status.get("completed"):
                raise _Failure("The render finished without an image", "empty_response")
        raise _Failure(f"No result within {int(_timeout_s())} s (prompt {prompt_id} may still be running)",
                       "timeout")


class _Failure(Exception):
    def __init__(self, message: str, error_type: str):
        super().__init__(message)
        self.error_type = error_type


def register(ctx: Any) -> None:
    ctx.register_image_gen_provider(ComfyUIImageGenProvider())
