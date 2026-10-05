"""The CPU fallback reads images too, so vision survives a GPU lease.

`local-chat` fails over to `llamacpp-cpu` while a render holds the GPU. Before 2026-10-04 that server
loaded no projector, so any image failed for the length of every render ("image input is not
supported"). It now loads its model's own projector. These tests keep the three places that
describe it in step: the catalog pins the file (so `ordo fetch` provisions it), the CPU service
passes that same file as --mmproj, and the gateway advertises vision for both CPU names.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ordo.render import engine
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.models_volume import required_model_files
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0", "uuid": "GPU-aaaa"}],
                "ram_gb": 128, "cpu_cores": 48, "platform": "Linux"}


def _fallback_projector_file() -> str:
    entry = CATALOG.by_file(engine.CPU_FALLBACK_DEFAULT_FILE)
    projector = entry.projector
    assert projector.source and projector.sha256, "the CPU fallback's projector must be pinned to download"
    return projector.file


def _cpu_command() -> list[str]:
    rc = render(Source.from_dict({"hardware": PROFILE_5090, "model": "qwen3.8-27b-heretic-ara-ninfer",
                                  "plugins": ["llamacpp-cpu"]}), CATALOG, REGISTRY)
    return [str(arg) for arg in rc.compose_dict()["services"]["llamacpp-cpu"]["command"]], rc


def test_the_cpu_service_loads_the_catalog_projector():
    command, _ = _cpu_command()
    assert command[command.index("--mmproj") + 1] == f"/models/{_fallback_projector_file()}"


def test_the_projector_is_a_required_model_file_so_fetch_provisions_it():
    _, rc = _cpu_command()
    needed = required_model_files(rc.compose_dict(), rc.env, ["llamacpp-cpu"])
    assert _fallback_projector_file() in {n.file for n in needed if not n.optional}


def test_the_gateway_advertises_vision_for_both_cpu_names():
    config = yaml.safe_load((ROOT / "services" / "model-gateway" / "litellm_config.yaml").read_text())
    cpu = [m for m in config["model_list"] if m["litellm_params"].get("api_base", "").startswith("http://llamacpp-cpu")]
    assert len(cpu) == 2
    assert all(m["model_info"]["supports_vision"] is True for m in cpu)
