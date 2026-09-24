"""The models the stack serves are derived from the render, never from a second record.

`data/ops-controller/model-registry.json` used to answer /registry/models and the dashboard's
delete guard. Nothing had written it since GPU assignment went 410, so it only matched the render
by coincidence: after a switch it would still name the old projector. These pin the replacement:
every answer comes from the same render `ordo.yaml` produces, and a stale file changes nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ordo.broker import Broker, MockBackend
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.control import ControlPlane
from ordo.plugins import PluginRegistry
from ordo.render import render
from ordo.scheduler import Scheduler
from ordo.served_models import model_files, served_models

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
PLUGINS = PluginRegistry.load(ROOT / "services")

UUID_BIG = "GPU-big-0000"
UUID_SMALL = "GPU-small-0000"
DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": UUID_BIG, "compute_cap": "12.0"},
                 {"name": "GTX 1070", "vram_gb": 8, "uuid": UUID_SMALL, "compute_cap": "6.1"}],
        "ram_gb": 128, "cpu_cores": 32}
CPU_FALLBACK_FILE = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
EMBED_FILE = "nomic-embed-text-v1.5.Q4_K_M.gguf"

# Two catalog models whose vision projectors differ: the switch case the stale registry got wrong.
MODEL_A = "qwen3.8-27b-turbo-fable-q6"
MODEL_B = "qwen3.8-27b-uncensored-q6"


def _source(model: str) -> dict:
    return {"hardware": DUAL, "model": model, "plugins": "auto"}


def _cp(tmp_path: Path, model: str = MODEL_A) -> ControlPlane:
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(_source(model)), encoding="utf-8")
    sched = Scheduler(32)
    return ControlPlane(src, CATALOG, PLUGINS, tmp_path / "out", scheduler=sched,
                        broker=Broker(sched, MockBackend()))


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def test_the_two_test_models_have_different_projectors():
    a, b = CATALOG.get(MODEL_A), CATALOG.get(MODEL_B)
    assert a.mmproj and b.mmproj and a.mmproj != b.mmproj


def test_model_files_are_every_file_the_render_loads():
    rc = render(Source.from_dict(_source(MODEL_A)), CATALOG, PLUGINS)
    files = {f.file: f for f in model_files(rc.compose_dict(), rc.env)}
    model = CATALOG.get(MODEL_A)
    assert files[model.file].service == "llamacpp" and not files[model.file].optional
    assert files[_basename(model.mmproj)].optional
    assert files[CPU_FALLBACK_FILE].service == "llamacpp-cpu"
    assert files[EMBED_FILE].service == "llamacpp-embed"


def test_served_models_come_from_the_render():
    rc = render(Source.from_dict(_source(MODEL_A)), CATALOG, PLUGINS)
    models = served_models(rc.compose_dict(), rc.env, rc.gpu_inventory())
    model = CATALOG.get(MODEL_A)

    chat = models["local-chat"]
    assert chat["service"] == "llamacpp" and chat["kind"] == "chat"
    assert chat["source"] == {"file": model.file}
    assert chat["config"] == {"ctx": rc.ctx_size, "mmproj": model.mmproj}
    assert chat["gpu_uuid"] == UUID_BIG
    assert chat["est_vram_gb"] == rc.resident_vram_gb()

    assert models["local-chat-cpu"]["source"] == {"file": CPU_FALLBACK_FILE}
    assert models["local-chat-cpu"]["gpu_uuid"] is None
    assert models["local-embed"]["source"] == {"file": EMBED_FILE}
    assert models["local-embed"]["gpu_uuid"] == UUID_BIG
    assert models["comfyui"]["gpu_uuid"] == UUID_BIG
    assert models["voice-stt"]["gpu_uuid"] == UUID_SMALL
    assert models["voice-stt"]["source"] == {"file": "Systran/faster-whisper-small"}
    assert models["voice-tts"]["gpu_uuid"] == UUID_SMALL


def test_a_service_the_render_does_not_run_has_no_record():
    rc = render(Source.from_dict({**_source(MODEL_A), "plugins": []}), CATALOG, PLUGINS)
    models = served_models(rc.compose_dict(), rc.env, rc.gpu_inventory())
    assert "local-chat" in models
    assert "voice-stt" not in models and "comfyui" not in models


def test_model_config_names_the_projector_and_every_loaded_file(tmp_path):
    cp = _cp(tmp_path)
    code, body = cp.route("GET", "/model-config")
    assert code == 200
    model = CATALOG.get(MODEL_A)
    assert body["active_mmproj"] == _basename(model.mmproj)
    loaded = {f["file"] for f in body["model_files"]}
    assert {model.file, _basename(model.mmproj), CPU_FALLBACK_FILE, EMBED_FILE} <= loaded


def test_registry_models_follow_a_switch_with_no_file_written(tmp_path):
    cp = _cp(tmp_path)
    assert cp.route("POST", "/model-config", {"model": MODEL_B})[0] == 200
    code, body = cp.route("GET", "/registry/models")
    assert code == 200
    chat = body["models"]["local-chat"]
    assert chat["source"]["file"] == CATALOG.get(MODEL_B).file
    assert chat["config"]["mmproj"] == CATALOG.get(MODEL_B).mmproj
    assert not list(tmp_path.rglob("model-registry.json"))


def test_a_stale_registry_file_changes_nothing(tmp_path, monkeypatch):
    stale = tmp_path / "model-registry.json"
    stale.write_text(json.dumps({"version": 1, "models": {"local-chat": {
        "id": "local-chat", "kind": "chat", "service": "llamacpp", "runtime": "single-model",
        "source": {"file": "old.gguf"}, "config": {"mmproj": "/models/old-vision.gguf"}}}}),
        encoding="utf-8")
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(stale))
    cp = _cp(tmp_path)
    chat = cp.route("GET", "/registry/models")[1]["models"]["local-chat"]
    assert chat["source"]["file"] == CATALOG.get(MODEL_A).file
    assert chat["config"]["mmproj"] == CATALOG.get(MODEL_A).mmproj


def test_registry_gpus_list_the_models_the_render_pins_to_each_card(tmp_path, monkeypatch):
    cp = _cp(tmp_path)
    live = {UUID_BIG: {"name": "RTX 5090", "total_gb": 32.0, "used_gb": 1.0, "util": 0},
            UUID_SMALL: {"name": "GTX 1070", "total_gb": 8.0, "used_gb": 1.0, "util": 0}}
    monkeypatch.setattr(cp, "_live_gpus", lambda: live)
    gpus = cp.route("GET", "/registry/gpus")[1]["gpus"]
    assert {"local-chat", "local-embed", "comfyui"} <= set(gpus[UUID_BIG]["models"])
    assert set(gpus[UUID_SMALL]["models"]) == {"voice-stt", "voice-tts"}
    assert gpus[UUID_BIG]["name"] == "RTX 5090"


def test_ops_controller_carries_no_registry_path():
    rc = render(Source.from_dict(_source(MODEL_A)), CATALOG, PLUGINS)
    env = rc.compose_dict()["services"]["ops-controller"]["environment"]
    assert "MODEL_REGISTRY_PATH" not in env
