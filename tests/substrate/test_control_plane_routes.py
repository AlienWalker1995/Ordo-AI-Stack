"""Characterization tests for every ops-controller route.

These record what the code DOES today, not what it should do. They must pass
against the unmodified code. If one fails, the route behaves differently than
expected — that is a finding, not a test bug.
"""
from pathlib import Path

import yaml

from ordo.broker import Broker, MockBackend
from ordo.catalog import Catalog
from ordo.control import ControlPlane
from ordo.lease_history import LeaseHistory
from ordo.plugins import PluginRegistry
from ordo.scheduler import Scheduler

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


def _cp(tmp_path, model="auto", with_broker=True):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": model, "plugins": "auto"}
    ))
    sched = Scheduler(32)
    broker = Broker(sched, MockBackend()) if with_broker else None
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=sched, broker=broker), src


# --- GET /status ---

def test_get_status_returns_200_with_manifest_and_gpu(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/status")
    assert code == 200
    assert "manifest" in body
    assert "gpu" in body
    assert body["gpu"]["state"] == "idle"


# --- GET /model-config ---

def test_get_model_config_returns_200_with_config_fields(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/model-config")
    assert code == 200
    assert "source_model" in body
    assert "active_model" in body
    assert "tier" in body
    assert "ctx_size" in body
    assert "available" in body
    assert body["source_model"] == "auto"


def test_get_model_config_names_the_file_being_served(tmp_path):
    """Consumers that key by GGUF file (throughput attribution, the dashboard's installed check)
    need the served file, not only the catalog id; guessing it from the id is what went wrong."""
    cp, _ = _cp(tmp_path, model="qwen3.8-27b-turbo-fable-q6")
    code, body = cp.route("GET", "/model-config")
    assert code == 200
    assert body["active_model"] == "qwen3.8-27b-turbo-fable-q6"
    assert body["active_file"] == CATALOG.get("qwen3.8-27b-turbo-fable-q6").file
    files = {m["id"]: m["file"] for m in body["available"]}
    assert files["qwen3.8-27b-turbo-fable-q6"] == body["active_file"]
    assert all(m["file"] for m in body["available"])


# --- POST /model-config ---

def test_post_model_config_valid_model_returns_200(tmp_path):
    cp, src = _cp(tmp_path)
    target = CATALOG.models[0].id
    code, body = cp.route("POST", "/model-config", {"model": target})
    assert code == 200
    assert body["ok"] is True
    assert body["active_model"] == target
    assert "ctx_size" in body
    assert "warnings" in body
    assert "wrote" in body


def test_post_model_config_missing_model_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/model-config", {})
    assert code == 400
    assert "error" in body


def test_post_model_config_unknown_model_returns_404(tmp_path):
    cp, src = _cp(tmp_path)
    before = src.read_text()
    code, body = cp.route("POST", "/model-config", {"model": "does-not-exist"})
    assert code == 404
    assert "error" in body
    assert "available" in body
    assert src.read_text() == before


# --- GET /plugins ---

def test_get_plugins_returns_200_with_plugins_list(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/plugins")
    assert code == 200
    assert "plugins" in body
    assert isinstance(body["plugins"], list)
    # Each plugin view has these keys
    if body["plugins"]:
        p = body["plugins"][0]
        assert "id" in p
        assert "name" in p
        assert "description" in p
        assert "services" in p
        assert "compose_profile" in p
        assert "secrets" in p
        assert "missing_secrets" in p
        assert "fits" in p
        assert "enabled" in p


# --- POST /plugins/{id}/enable ---

def test_post_plugin_enable_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/comfyui/enable", {})
    assert code == 200
    assert body["ok"] is True
    assert body["plugin"] == "comfyui"
    assert "services" in body
    assert "compose_profile" in body
    assert "wants_secrets" in body
    assert "missing_secrets" in body
    assert "warnings" in body


def test_post_plugin_enable_not_installable_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/llamacpp/enable", {})
    assert code == 403
    assert "error" in body
    assert "installable" in body


def test_post_plugin_enable_unknown_returns_403(tmp_path):
    # The installability check (403) comes before the registry lookup (404).
    # An unknown plugin not in INSTALLABLE_PLUGINS returns 403.
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/does-not-exist/enable", {})
    assert code == 403
    assert "error" in body
    assert "installable" in body


# --- POST /plugins/{id}/disable ---

def test_post_plugin_disable_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/comfyui/disable", {})
    assert code == 200
    assert body["ok"] is True
    assert body["plugin"] == "comfyui"
    assert "services" in body


def test_post_plugin_disable_not_installable_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/llamacpp/disable", {})
    assert code == 403
    assert "error" in body


def test_post_plugin_disable_unknown_returns_403(tmp_path):
    # The installability check (403) comes before the registry lookup (404).
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/does-not-exist/disable", {})
    assert code == 403
    assert "error" in body


# --- POST /jobs ---

def test_post_job_valid_returns_200_with_status(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs", {"id": "render", "vram_gb": 17, "kind": "media"})
    assert code == 200
    assert "state" in body
    assert "running" in body


def test_post_job_missing_vram_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs", {"id": "x"})
    assert code == 400
    assert "error" in body


def test_post_job_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    code, body = cp.route("POST", "/jobs", {"id": "x", "vram_gb": 1})
    assert code == 503
    assert "error" in body


# --- POST /jobs/complete ---

def test_post_job_complete_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    cp.route("POST", "/jobs", {"id": "render", "vram_gb": 17, "kind": "media"})
    code, body = cp.route("POST", "/jobs/complete", {"id": "render"})
    assert code == 200
    assert "state" in body


def test_post_job_complete_missing_id_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs/complete", {})
    assert code == 400
    assert "error" in body


def test_post_job_complete_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    code, body = cp.route("POST", "/jobs/complete", {"id": "x"})
    assert code == 503
    assert "error" in body


# --- POST /jobs/heartbeat ---

def test_post_job_heartbeat_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    cp.route("POST", "/jobs", {"id": "train", "vram_gb": 30, "kind": "training"})
    code, body = cp.route("POST", "/jobs/heartbeat", {"id": "train"})
    assert code == 200
    assert "state" in body


def test_post_job_heartbeat_unknown_job_returns_404(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs/heartbeat", {"id": "ghost"})
    assert code == 404
    assert "error" in body


def test_post_job_heartbeat_missing_id_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs/heartbeat", {})
    assert code == 400
    assert "error" in body


def test_post_job_heartbeat_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    code, body = cp.route("POST", "/jobs/heartbeat", {"id": "x"})
    assert code == 503
    assert "error" in body


# --- GET /jobs/history ---

def test_get_jobs_history_returns_200_with_history(tmp_path):
    cp, _ = _cp(tmp_path)
    hist = LeaseHistory(tmp_path / "h.jsonl", now_fn=lambda: 42.0)
    cp.broker.history = hist
    cp.history = hist
    cp.route("POST", "/jobs", {"id": "render", "vram_gb": 17, "kind": "media"})
    cp.route("POST", "/jobs/complete", {"id": "render"})
    code, body = cp.route("GET", "/jobs/history")
    assert code == 200
    assert "history" in body
    assert isinstance(body["history"], list)
    assert body["history"][0]["id"] == "render"
    assert body["history"][0]["outcome"] == "completed"


def test_get_jobs_history_empty_without_sink(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/jobs/history")
    assert code == 200
    assert body == {"history": []}


# --- GET /health ---

def test_get_health_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/health")
    assert code == 200
    assert body == {"ok": True}


# --- GET /healthz ---

def test_get_healthz_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/healthz")
    assert code == 200
    assert body == {"ok": True}


# --- Unknown path ---

def test_unknown_path_returns_404(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/nope")
    assert code == 404
    assert "error" in body
    assert "no route" in body["error"]


def _cp_explicit(tmp_path, plugins):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "plugins": plugins}
    ))
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=Scheduler(32)), src


def test_post_plugin_enable_missing_site_key_explicit_list_returns_409(tmp_path):
    # memory-vault needs site.MEMORY_VAULT_PATH: the render refuses, so nothing is written.
    cp, src = _cp_explicit(tmp_path, ["rag"])
    before = src.read_text()
    code, body = cp.route("POST", "/plugins/memory-vault/enable", {})
    assert code == 409
    assert "MEMORY_VAULT_PATH" in body["error"]
    assert src.read_text() == before


def test_post_plugin_enable_missing_site_key_auto_returns_409_naming_key(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/plugins/memory-vault/enable", {})
    assert code == 409
    assert "MEMORY_VAULT_PATH" in body["error"]
