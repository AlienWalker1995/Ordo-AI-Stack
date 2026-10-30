"""Model engines are declared once and found by their labels, never by a list of service names.

A service that serves a model declares `serves:` (the core chat engine is declared by the render).
The render stamps that on the service as `ordo.serves.*` labels, and the registry reads the labels,
so an engine a third-party plugin adds is served, listed and measured with no code change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo.render import engines
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry, PluginService
from ordo.render.served_models import served_models

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
PLUGINS = PluginRegistry.load(ROOT / "services")

GPU_HOST = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-big-0000", "compute_cap": "12.0"}],
            "ram_gb": 128, "cpu_cores": 32}
CPU_HOST = {"gpus": [], "ram_gb": 64, "cpu_cores": 16}
LLAMACPP_MODEL = "qwen3.8-27b-turbo-fable-q6"
NINFER_MODEL = "qwen3.8-27b-nvfp4-ninfer"


def _models(hardware: dict, model: str | None = None, plugins="auto") -> dict:
    source = {"hardware": hardware, "plugins": plugins, **({"model": model} if model else {})}
    rc = render(Source.from_dict(source), CATALOG, PLUGINS)
    return served_models(rc.compose_dict(), rc.env, rc.gpu_inventory())


# --- the manifest declaration -------------------------------------------------------------------

def test_a_manifest_declares_what_a_service_serves():
    ps = PluginService.from_dict({"name": "x", "image": "i", "serves": {"model": "my-chat", "kind": "chat",
                                                                       "metrics": "llamacpp", "port": 8080}})
    assert ps.serves == engines.Serves(model="my-chat", kind="chat", metrics="llamacpp", port=8080)


@pytest.mark.parametrize("serves,message", [
    ({"kind": "chat"}, "serves.model"),
    ({"model": "m", "kind": "video"}, "serves.kind"),
    ({"model": "m", "kind": "chat", "metrics": "prometheus"}, "serves.metrics"),
    ({"model": "m", "kind": "chat", "metrics_path": "metrics"}, "metrics_path"),
    ({"model": "m", "kind": "chat", "device": "gpu"}, "unknown keys"),
    ({"model": "m", "kind": "chat", "metrics": "llamacpp"}, "serves.port"),
])
def test_a_bad_declaration_is_refused(serves, message):
    with pytest.raises(ValueError, match=message):
        PluginService.from_dict({"name": "x", "image": "i", "serves": serves})


def test_the_device_comes_from_the_rendered_wiring_not_the_manifest():
    assert engines.device_of({"deploy": {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}}) == "gpu"
    assert engines.device_of({"devices": ["/dev/dri"]}) == "gpu"
    assert engines.device_of({"image": "i"}) == "cpu"


def test_labels_round_trip():
    svc: dict = {"image": "i"}
    engines.stamp(svc, engines.Serves(model="m", kind="chat", metrics="llamacpp", fallback_for="local-chat"))
    assert engines.read(svc["labels"]) == {"model": "m", "kind": "chat", "device": "cpu", "metrics": "llamacpp",
                                           "metrics_path": "/metrics", "port": "", "fallback_for": "local-chat",
                                           "model_env": ""}
    assert engines.read({"com.example": "x"}) is None


# --- the registry reads labels, so any engine is found ------------------------------------------

def test_an_engine_no_code_knows_about_is_served():
    """A third-party plugin's engine: only its labels say it serves a model."""
    svc: dict = {"image": "vendor/engine", "command": ["--ctx-size", "16384"]}
    engines.stamp(svc, engines.Serves(model="vendor-chat", kind="chat", metrics="llamacpp", port=8080))
    models = served_models({"services": {"vendor-engine": svc}}, {}, [])
    assert models["vendor-chat"]["service"] == "vendor-engine"
    assert models["vendor-chat"]["device"] == "cpu"
    assert models["vendor-chat"]["config"] == {"ctx": 16384}
    assert models["vendor-chat"]["metrics"] == {"format": "llamacpp", "path": "/metrics", "port": 8080}


def test_the_registry_keeps_no_list_of_engine_services():
    source = (ROOT / "ordo" / "render" / "served_models.py").read_text(encoding="utf-8")
    for name in ("inference-engine-cpu", "inference-engine-embed", "comfyui", '"stt"', '"tts"'):
        assert name not in source, f"served_models.py names {name}; declare it with `serves:` instead"


# --- what the shipped manifests declare ---------------------------------------------------------

def test_the_gpu_host_engines():
    models = _models(GPU_HOST, LLAMACPP_MODEL)
    assert models["local-chat"]["device"] == "gpu"
    assert models["local-chat"]["metrics"]["format"] == "llamacpp"
    assert models["local-chat-cpu"]["device"] == "cpu"
    assert models["local-chat-cpu"]["fallback_for"] == "local-chat"
    assert models["local-chat-cpu"]["metrics"]["format"] == "llamacpp"
    assert models["local-embed"]["config"] == {"ctx": 8192}
    assert models["local-embed"]["metrics"]["format"] == "none"
    assert models["local-chat"]["fallback_for"] is None


def test_a_ninfer_chat_model_is_read_from_its_log():
    models = _models(GPU_HOST, NINFER_MODEL)
    assert models["local-chat"]["metrics"]["format"] == "ninfer-log"


def test_a_cpu_only_host_runs_the_chat_engine_on_cpu():
    models = _models(CPU_HOST, plugins=[])
    assert models["local-chat"]["device"] == "cpu"
