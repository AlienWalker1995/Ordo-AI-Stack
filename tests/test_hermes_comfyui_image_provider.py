"""Hermes' `comfyui` image_gen provider runs the repo's MiniMax-H3-Image workflow through the GPU gate.

The provider is loaded the way the image places it (services/hermes/Dockerfile copies it beside
upstream's bundled backends). Upstream's `agent.image_gen_provider` and `plugins.image_gen._common`
are not in this repo, so the few names it imports are stubbed with their documented behaviour, and
ComfyUI is a fake answering the gate's routes.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROVIDER_FILE = REPO / "services" / "hermes" / "plugins" / "image_gen" / "comfyui" / "__init__.py"
WORKFLOW_FILE = REPO / "scripts" / "comfyui" / "workflows" / "minimax-h3-image.json"
GATE = "http://comfyui-gate:8188"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _stub_hermes(monkeypatch, saved):
    agent = types.ModuleType("agent")
    provider_api = types.ModuleType("agent.image_gen_provider")

    class ImageGenProvider:
        pass

    def resolve_aspect_ratio(value):
        v = value.strip().lower() if isinstance(value, str) else ""
        return v if v in ("landscape", "square", "portrait") else "landscape"

    def save_b64_image(data, *, prefix="image", extension="png"):
        saved.append(base64.b64decode(data))
        return Path(f"/cache/{prefix}.{extension}")

    def success_response(*, image, model, prompt, aspect_ratio, provider, modality="text", extra=None):
        return {"success": True, "image": image, "model": model, "prompt": prompt,
                "aspect_ratio": aspect_ratio, "provider": provider, "modality": modality, **(extra or {})}

    provider_api.ImageGenProvider = ImageGenProvider
    provider_api.DEFAULT_ASPECT_RATIO = "landscape"
    provider_api.resolve_aspect_ratio = resolve_aspect_ratio
    provider_api.save_b64_image = save_b64_image
    provider_api.success_response = success_response

    plugins = types.ModuleType("plugins")
    image_gen = types.ModuleType("plugins.image_gen")
    common = types.ModuleType("plugins.image_gen._common")

    def collect_source_images(image_url, reference_image_urls):
        out = ([image_url] if image_url else []) + list(reference_image_urls or [])
        return out or None

    def error_factory(provider, aspect, *, model="", prompt=""):
        def fail(error, error_type, **override):
            return {"success": False, "error": error, "error_type": error_type, "provider": provider,
                    "model": model, "prompt": prompt, "aspect_ratio": aspect, **override}
        return fail

    # requests is Hermes' HTTP client; the fake ComfyUI replaces get/post per test.
    http = types.ModuleType("requests")
    http.RequestException = type("RequestException", (Exception,), {})

    common.collect_source_images = collect_source_images
    common.error_factory = error_factory
    common.load_image_gen_config = lambda sub=None: {}
    for name, module in {"agent": agent, "agent.image_gen_provider": provider_api, "plugins": plugins,
                         "plugins.image_gen": image_gen, "plugins.image_gen._common": common,
                         "requests": http}.items():
        monkeypatch.setitem(sys.modules, name, module)


class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body


class FakeComfyUI:
    """The gate's routes the provider uses, answering from the repo's own workflow file."""

    def __init__(self, history=None, submit_status=200):
        self.workflow = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
        self.history = history
        self.submit_status = submit_status
        self.submitted = None
        self.uploads = []
        self.urls = []

    def get(self, url, timeout=None, params=None, stream=False):
        self.urls.append(url)
        if url == f"{GATE}/api/userdata/workflows%2Fordo%2Fminimax-h3-image.json":
            return FakeResponse(body=self.workflow)
        if url == f"{GATE}/history/p-1":
            return FakeResponse(body=self.history if self.history is not None else {"p-1": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"8": {"images": [{"filename": "minimax-h3-image_00001_.png",
                                              "subfolder": "image", "type": "output"}]}}}})
        if url == f"{GATE}/view":
            assert params == {"filename": "minimax-h3-image_00001_.png", "subfolder": "image", "type": "output"}
            return FakeResponse(content=PNG)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None, files=None, data=None, timeout=None):
        self.urls.append(url)
        if url == f"{GATE}/upload/image":
            name, payload, _mime = files["image"]
            self.uploads.append(payload)
            return FakeResponse(body={"name": name, "subfolder": "", "type": "input"})
        if url == f"{GATE}/prompt":
            self.submitted = json["prompt"]
            if self.submit_status != 200:
                return FakeResponse(status=self.submit_status, body={"error": "GPU busy"})
            return FakeResponse(body={"prompt_id": "p-1"})
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def load(monkeypatch):
    saved = []
    _stub_hermes(monkeypatch, saved)
    monkeypatch.setenv("COMFYUI_URL", GATE)

    def _load(fake):
        spec = importlib.util.spec_from_file_location("comfyui_image_provider_under_test", PROVIDER_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.requests.get = fake.get
        module.requests.post = fake.post
        monkeypatch.setattr(module.time, "sleep", lambda _s: None)
        captured = []
        module.register(types.SimpleNamespace(register_image_gen_provider=captured.append))
        return captured[0], saved

    return _load


def test_the_workflow_has_the_nodes_the_provider_fills_in():
    graph = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    # The node ids are module constants; read them without importing Hermes.
    source = PROVIDER_FILE.read_text(encoding="utf-8")
    ids = dict(re.findall(r'^(PROMPT_NODE|SAMPLER_NODE|SAVE_NODE) = "(\w+)"', source, flags=re.M))
    assert set(ids) == {"PROMPT_NODE", "SAMPLER_NODE", "SAVE_NODE"}
    assert graph[ids["PROMPT_NODE"]]["class_type"] == "MiniMaxH3ImageConditioning"
    assert graph[ids["SAMPLER_NODE"]]["class_type"] == "KSampler"
    assert graph[ids["SAVE_NODE"]]["class_type"] == "SaveImage"
    # The distilled schedule: 6 steps at cfg 1 (other values are wrong for this model).
    assert graph[ids["SAMPLER_NODE"]]["inputs"]["steps"] == 6
    assert graph[ids["SAMPLER_NODE"]]["inputs"]["cfg"] == 1


def test_text_to_image_runs_the_repo_workflow_through_the_gate(load):
    fake = FakeComfyUI()
    provider, saved = load(fake)
    result = provider.generate("A detailed harbour at dawn", "portrait", seed=7)

    assert result["success"] is True
    assert result["provider"] == "comfyui" and result["model"] == "minimax-h3-image"
    assert saved == [PNG]
    graph = fake.submitted
    assert "_comment" not in graph                        # ComfyUI rejects non-node keys
    assert graph["4"]["inputs"]["prompt"] == "A detailed harbour at dawn"
    assert (graph["4"]["inputs"]["width"], graph["4"]["inputs"]["height"]) == (1536, 2720)
    assert graph["6"]["inputs"]["seed"] == 7
    assert all(url.startswith(GATE) for url in fake.urls)   # never comfyui:8188 directly


def test_an_edit_uploads_each_picture_and_keeps_its_size(load):
    fake = FakeComfyUI()
    provider, _saved = load(fake)
    picture = "data:image/png;base64," + base64.b64encode(b"source-bytes").decode()
    result = provider.generate("Make <Picture 1> a watercolour", "landscape", image_url=picture)

    assert result["success"] is True and result["modality"] == "image"
    assert fake.uploads == [b"source-bytes"]
    graph = fake.submitted
    load_node = graph["4"]["inputs"]["images.image_1"]
    assert graph[load_node[0]]["class_type"] == "LoadImage"
    assert (graph["4"]["inputs"]["width"], graph["4"]["inputs"]["height"]) == (0, 0)


def test_a_render_that_fails_in_comfyui_reports_why(load):
    fake = FakeComfyUI(history={"p-1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"exception_message": "CUDA out of memory"}]]}, "outputs": {}}})
    provider, saved = load(fake)
    result = provider.generate("anything detailed", "square")

    assert result["success"] is False and result["error_type"] == "api_error"
    assert "CUDA out of memory" in result["error"]
    assert saved == []


def test_the_gate_refusing_the_gpu_is_an_error_not_a_hang(load):
    fake = FakeComfyUI(submit_status=503)
    provider, _saved = load(fake)
    result = provider.generate("anything detailed", "square")

    assert result["success"] is False and "503" in result["error"]


def test_without_the_gate_the_provider_is_unavailable(load, monkeypatch):
    provider, _saved = load(FakeComfyUI())
    monkeypatch.delenv("COMFYUI_URL")
    assert provider.is_available() is False
    assert provider.generate("x", "square")["error_type"] == "not_configured"
