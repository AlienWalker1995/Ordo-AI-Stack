"""Rendered compose is isolated + correct so it can run beside the live stack."""
from pathlib import Path

import yaml

from ordo import compose
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")


def test_core_services_present():
    c = compose.render_compose(has_gpu=True, compose_profiles=["media", "voice"])
    for s in compose.core_services() + ["agent"]:
        assert s in c["services"]


def test_isolated_no_port_clashes():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    assert c["name"] == "ordo-v2"
    assert "ordo-v2-net" in c["networks"]
    for name, svc in c["services"].items():
        assert "ports" not in svc, f"{name} publishes a host port (would clash)"
        assert "container_name" not in svc, f"{name} pins a name (would clash)"
        assert svc["networks"] == ["ordo-v2-net"]


def test_gpu_reservation_gated_by_hardware():
    with_gpu = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert "deploy" in with_gpu["services"]["llamacpp"]
    no_gpu = compose.render_compose(has_gpu=False, compose_profiles=[])
    assert "deploy" not in no_gpu["services"]["llamacpp"]


def test_plugin_services_behind_profiles():
    c = compose.render_compose(has_gpu=True, compose_profiles=["media"])
    assert c["services"]["comfyui"]["profiles"] == ["media"]
    assert "voice" not in c["services"]                       # voice profile not enabled
    c2 = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert "comfyui" not in c2["services"]


def test_ops_controller_has_scoped_socket():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    ops = c["services"]["ops-controller"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in ops["volumes"]  # drives the broker
    # but it's launched scoped to the project — the guard can't reach ordo-ai-stack-*
    assert "--project" in ops["command"] and "ordo-v2" in ops["command"]


def test_agent_swappable():
    c = compose.render_compose(has_gpu=False, compose_profiles=[], agent="openclaw")
    assert "agent-openclaw" in c["services"]["agent"]["image"]


def test_llamacpp_image_defaults_to_upstream():
    c = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert c["services"]["llamacpp"]["image"] == "ghcr.io/ggml-org/llama.cpp:server"


def test_llamacpp_image_override():
    patched = "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470"
    c = compose.render_compose(has_gpu=True, compose_profiles=[], llamacpp_image=patched)
    assert c["services"]["llamacpp"]["image"] == patched


def test_render_writes_runnable_compose(tmp_path):
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    render(src, CATALOG, REGISTRY).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert "llamacpp" in c["services"] and "agent" in c["services"]
    assert c["services"]["comfyui"]["profiles"] == ["media"]   # media enabled on 5090
    assert "deploy" in c["services"]["llamacpp"]               # GPU reserved


def test_backend_image_flows_from_catalog_to_compose_and_env(tmp_path):
    # the 5090 best-fits huihui-qwen3.6-27b-q6, whose catalog entry pins the patched build
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.backend_image == "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470"
    assert rc.env["LLAMACPP_IMAGE"] == rc.model.backend_image
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["llamacpp"]["image"] == rc.model.backend_image


def test_model_without_backend_image_keeps_default(tmp_path):
    # a small GPU best-fits a stock model (no backend_image) -> upstream image, no LLAMACPP_IMAGE
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 8}], "ram_gb": 32},
                            "model": "auto", "plugins": "auto"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.backend_image is None
    assert "LLAMACPP_IMAGE" not in rc.env
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["llamacpp"]["image"] == "ghcr.io/ggml-org/llama.cpp:server"
