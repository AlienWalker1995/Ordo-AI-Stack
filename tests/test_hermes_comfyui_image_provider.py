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
import os
import re
import shutil
import subprocess
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
    monkeypatch.setenv("IMAGE_GEN_ENABLED", "1")

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


def test_with_image_gen_disabled_the_provider_never_touches_the_gpu(load, monkeypatch):
    """Selected earlier, then image-gen turned off: the weights may be gone, so a call must fail
    before it reaches the gate (whose /prompt would take the lease and evict the chat model)."""
    fake = FakeComfyUI()
    provider, _saved = load(fake)
    monkeypatch.delenv("IMAGE_GEN_ENABLED")
    assert provider.is_available() is False
    result = provider.generate("anything detailed", "square")
    assert result["error_type"] == "not_configured"
    assert fake.urls == []


def test_without_the_gate_the_provider_is_unavailable(load, monkeypatch):
    provider, _saved = load(FakeComfyUI())
    monkeypatch.delenv("COMFYUI_URL")
    assert provider.is_available() is False
    assert provider.generate("x", "square")["error_type"] == "not_configured"


# --- How the image places the provider, and how the boot hook selects it ----------------------

HERMES_DIR = REPO / "services" / "hermes"


def test_the_image_copies_the_provider_beside_upstreams_bundled_backends():
    """Upstream discovers image_gen backends under /opt/hermes/plugins/image_gen/<name>/, so the
    COPY must land exactly there and the source must hold a loadable plugin."""
    dockerfile = (HERMES_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY plugins/image_gen/comfyui/ /opt/hermes/plugins/image_gen/comfyui/" in dockerfile
    assert "/opt/hermes/plugins/image_gen/comfyui " in dockerfile          # readable by the hermes user
    source = HERMES_DIR / "plugins" / "image_gen" / "comfyui"
    assert (source / "__init__.py").is_file()
    manifest = (source / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: comfyui" in manifest and "kind: backend" in manifest


def _hook_block() -> str:
    """The image-gen block of the boot hook, from its sentinel to the `fi` that closes it."""
    lines = (HERMES_DIR / "ordo-init.sh").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("IMAGE_GEN_MARK="))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1])


FAKE_HERMES = """#!/bin/sh
# `hermes config get|set KEY [VALUE]` over one file per key, failing `get` on an unset key as the
# real CLI does ("Config key not set").
store="$FAKE_HERMES_STORE"
[ "$1" = config ] || exit 2
case "$2" in
  get) [ -f "$store/$3" ] && cat "$store/$3" || exit 1 ;;
  set) printf '%s' "$4" > "$store/$3" ;;
esac
"""


@pytest.fixture
def boot(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("needs bash to run the boot hook block")
    fake = tmp_path / "hermes"
    fake.write_text(FAKE_HERMES, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    store = tmp_path / "config"
    store.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "hook.sh"
    script.write_text("as_hermes() { \"$@\"; }\n"
                      "hermes_config() { as_hermes \"$HERMES_BIN\" config set \"$1\" \"$2\" >/dev/null; }\n"
                      + _hook_block() + "\n", encoding="utf-8", newline="\n")

    def run(**env):
        full = {"PATH": os.environ.get("PATH", ""), "HERMES_BIN": str(fake), "HERMES_HOME": str(home),
                "FAKE_HERMES_STORE": str(store), **env}
        subprocess.run([bash, str(script)], env=full, check=True)
        provider = store / "image_gen.provider"
        return provider.read_text(encoding="utf-8") if provider.exists() else None

    run.store = store
    run.mark = home / ".ordo-image-gen-seeded"
    return run


def test_the_hook_selects_comfyui_when_image_gen_is_enabled(boot):
    assert boot(IMAGE_GEN_ENABLED="1", COMFYUI_URL=GATE) == "comfyui"
    assert boot.mark.exists()


def test_the_hook_does_not_override_the_operators_later_choice(boot):
    boot(IMAGE_GEN_ENABLED="1", COMFYUI_URL=GATE)
    (boot.store / "image_gen.provider").write_text("krea", encoding="utf-8")
    assert boot(IMAGE_GEN_ENABLED="1", COMFYUI_URL=GATE) == "krea"


def test_the_hook_keeps_a_provider_chosen_before_the_upgrade(boot):
    (boot.store / "image_gen.provider").write_text("openai", encoding="utf-8")
    assert boot(IMAGE_GEN_ENABLED="1", COMFYUI_URL=GATE) == "openai"
    assert boot.mark.exists()


def test_an_empty_provider_value_counts_as_none_chosen(boot):
    """`config get` succeeds on a key set to "", which must not read as a chosen provider: the tool
    would stay on FAL, which has no key here."""
    (boot.store / "image_gen.provider").write_text("", encoding="utf-8")
    assert boot(IMAGE_GEN_ENABLED="1", COMFYUI_URL=GATE) == "comfyui"


def test_comfyui_without_image_gen_selects_nothing(boot):
    """Without the image-gen weights every call would take the GPU lease (evicting the chat model)
    only to be rejected by ComfyUI, so ComfyUI alone must not select the provider."""
    assert boot(COMFYUI_URL=GATE) is None
    assert not boot.mark.exists()
