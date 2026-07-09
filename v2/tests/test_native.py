"""Native path builds the same llama.cpp config as the container from one rendered source."""
from pathlib import Path

from ordo import native
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")

GPU = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                        "model": "huihui-qwen3.6-27b-q6", "plugins": ["comfyui"]})


def _argv(env):
    return native.llama_server_argv(env, models_dir="/models")


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_maps_core_flags():
    argv = _argv({"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_CTX_SIZE": "131072",
                  "LLAMACPP_GPU_LAYERS": "-1", "LLAMACPP_KV_CACHE_TYPE_K": "q8_0",
                  "LLAMACPP_KV_CACHE_TYPE_V": "q8_0", "LLAMACPP_PARALLEL": "1"})
    assert argv[0] == "llama-server"
    assert _flag(argv, "--model") == "/models/m.gguf"
    assert _flag(argv, "--ctx-size") == "131072"
    assert _flag(argv, "--n-gpu-layers") == "-1"
    assert _flag(argv, "--cache-type-k") == "q8_0"


def test_extra_args_are_split_and_appended():
    argv = _argv({"LLAMACPP_MODEL": "m.gguf",
                  "LLAMACPP_EXTRA_ARGS": "--spec-type draft-mtp --spec-draft-n-max 10"})
    assert "--spec-type" in argv and "draft-mtp" in argv and "10" in argv


def test_rope_none_is_omitted():
    argv = _argv({"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_ROPE_SCALING": "none"})
    assert "--rope-scaling" not in argv


def test_mmproj_included_when_set():
    argv = _argv({"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_MMPROJ": "/models/mmproj.gguf"})
    assert _flag(argv, "--mmproj") == "/models/mmproj.gguf"


def test_native_config_matches_the_rendered_container_config():
    rc = render(GPU, CATALOG, REGISTRY)
    p = native.plan(rc, models_dir="/models")
    # the SAME ctx value the container gets — one source, two deployment modes, no divergence
    assert _flag(p.llama_server, "--ctx-size") == rc.env["LLAMACPP_CTX_SIZE"]
    assert _flag(p.llama_server, "--model") == "/models/" + rc.env["LLAMACPP_MODEL"]
    # the 27b's MTP spec-decode extra args carry into native too
    assert "--spec-type" in p.llama_server


def test_plan_is_honest_about_docker_only_pieces():
    rc = render(GPU, CATALOG, REGISTRY)      # comfyui enabled -> media profile
    p = native.plan(rc, models_dir="/models")
    assert any("media" in w for w in p.warnings)                 # media is Docker-only, said so
    assert any("gateway" in s.lower() for s in p.manual_steps)   # gateways are manual native
    txt = p.as_text()
    assert "llama-server" in txt and "best-effort" in txt
