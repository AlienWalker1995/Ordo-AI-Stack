"""ASGI transport tests for the ops-controller FastAPI app.

These drive the FastAPI application through TestClient and assert the same
responses that test_control_plane_routes.py asserts against route() directly.
"""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.lease_history import LeaseHistory
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

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


TOKEN = "asgi-test-token"


def _client(cp):
    # Every call authenticates; the auth rules themselves are pinned in test_control_plane_auth.py.
    return TestClient(cp.app(auth_token=TOKEN), headers={"Authorization": f"Bearer {TOKEN}"})


# --- GET /status ---

def test_get_status_returns_200_with_manifest_and_gpu(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "manifest" in body
    assert "gpu" in body
    assert body["gpu"]["state"] == "idle"


# --- GET /model-config ---

def test_get_model_config_returns_200_with_config_fields(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/model-config")
    assert resp.status_code == 200
    body = resp.json()
    assert "source_model" in body
    assert "active_model" in body
    assert "tier" in body
    assert "ctx_size" in body
    assert "available" in body
    assert body["source_model"] == "auto"


# --- POST /model-config ---

def test_post_model_config_valid_model_returns_200(tmp_path):
    cp, src = _cp(tmp_path)
    client = _client(cp)
    target = CATALOG.models[0].id
    resp = client.post("/model-config", json={"model": target})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["active_model"] == target
    assert "ctx_size" in body
    assert "warnings" in body
    assert "wrote" in body


def test_post_model_config_missing_model_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/model-config", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_model_config_unknown_model_returns_404(tmp_path):
    cp, src = _cp(tmp_path)
    client = _client(cp)
    before = src.read_text()
    resp = client.post("/model-config", json={"model": "does-not-exist"})
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "available" in body
    assert src.read_text() == before


# --- GET /plugins ---

def test_get_plugins_returns_200_with_plugins_list(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/plugins")
    assert resp.status_code == 200
    body = resp.json()
    assert "plugins" in body
    assert isinstance(body["plugins"], list)
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
    client = _client(cp)
    resp = client.post("/plugins/comfyui/enable", json={"confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["plugin"] == "comfyui"
    assert "services" in body
    assert "compose_profile" in body
    assert "wants_secrets" in body
    assert "missing_secrets" in body
    assert "warnings" in body


def test_post_plugin_enable_not_installable_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/plugins/llamacpp/enable", json={"confirm": True})
    assert resp.status_code == 403
    body = resp.json()
    assert "error" in body
    assert "installable" in body


def test_post_plugin_enable_unknown_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/plugins/does-not-exist/enable", json={"confirm": True})
    assert resp.status_code == 403
    body = resp.json()
    assert "error" in body
    assert "installable" in body


# --- POST /plugins/{id}/disable ---

def test_post_plugin_disable_under_plugins_auto_returns_409(tmp_path):
    # `plugins: auto` has no list item to remove, so a disable could not persist.
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/plugins/comfyui/disable", json={"confirm": True})
    assert resp.status_code == 409
    body = resp.json()
    assert body["plugin"] == "comfyui"
    assert "explicit `plugins:` list" in body["error"]


def test_post_plugin_disable_not_installable_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/plugins/llamacpp/disable", json={"confirm": True})
    assert resp.status_code == 403
    assert "error" in resp.json()


def test_post_plugin_disable_unknown_returns_403(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/plugins/does-not-exist/disable", json={"confirm": True})
    assert resp.status_code == 403
    assert "error" in resp.json()


# --- POST /jobs ---

def test_post_job_valid_returns_200_with_status(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/jobs", json={"id": "render", "vram_gb": 17, "kind": "media"})
    assert resp.status_code == 200
    body = resp.json()
    assert "state" in body
    assert "running" in body


def test_post_job_missing_vram_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/jobs", json={"id": "x"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_job_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    client = _client(cp)
    resp = client.post("/jobs", json={"id": "x", "vram_gb": 1})
    assert resp.status_code == 503
    assert "error" in resp.json()


# --- POST /jobs/complete ---

def test_post_job_complete_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    client.post("/jobs", json={"id": "render", "vram_gb": 17, "kind": "media"})
    resp = client.post("/jobs/complete", json={"id": "render"})
    assert resp.status_code == 200
    assert "state" in resp.json()


def test_post_job_complete_missing_id_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/jobs/complete", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_job_complete_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    client = _client(cp)
    resp = client.post("/jobs/complete", json={"id": "x"})
    assert resp.status_code == 503
    assert "error" in resp.json()


# --- POST /jobs/heartbeat ---

def test_post_job_heartbeat_valid_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    client.post("/jobs", json={"id": "train", "vram_gb": 30, "kind": "training"})
    resp = client.post("/jobs/heartbeat", json={"id": "train"})
    assert resp.status_code == 200
    assert "state" in resp.json()


def test_post_job_heartbeat_unknown_job_returns_404(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/jobs/heartbeat", json={"id": "ghost"})
    assert resp.status_code == 404
    assert "error" in resp.json()


def test_post_job_heartbeat_missing_id_returns_400(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.post("/jobs/heartbeat", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_job_heartbeat_no_broker_returns_503(tmp_path):
    cp, _ = _cp(tmp_path, with_broker=False)
    client = _client(cp)
    resp = client.post("/jobs/heartbeat", json={"id": "x"})
    assert resp.status_code == 503
    assert "error" in resp.json()


# --- GET /jobs/history ---

def test_get_jobs_history_returns_200_with_history(tmp_path):
    cp, _ = _cp(tmp_path)
    hist = LeaseHistory(tmp_path / "h.jsonl", now_fn=lambda: 42.0)
    cp.broker.history = hist
    cp.history = hist
    client = _client(cp)
    client.post("/jobs", json={"id": "render", "vram_gb": 17, "kind": "media"})
    client.post("/jobs/complete", json={"id": "render"})
    resp = client.get("/jobs/history")
    assert resp.status_code == 200
    body = resp.json()
    assert "history" in body
    assert isinstance(body["history"], list)
    assert body["history"][0]["id"] == "render"
    assert body["history"][0]["outcome"] == "completed"


def test_get_jobs_history_empty_without_sink(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/jobs/history")
    assert resp.status_code == 200
    assert resp.json() == {"history": []}


# --- GET /health ---

def test_get_health_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and resp.json()["substrate_digest"]


# --- GET /healthz ---

def test_get_healthz_returns_200(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and resp.json()["substrate_digest"]


# --- Unknown path ---

def test_unknown_path_returns_404(tmp_path):
    cp, _ = _cp(tmp_path)
    client = _client(cp)
    resp = client.get("/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "no route" in body["error"]


# --- Destructive routes confirm with JSON true only ---

# Every route that refuses to act without {"confirm": true}.
CONFIRM_GATED_ROUTES = [
    "/apply",
    "/services/llamacpp/start",
    "/services/llamacpp/stop",
    "/services/llamacpp/restart",
    "/services/llamacpp/recreate",
    "/containers/ordo-llamacpp-1/restart",
    "/compose/up",
    "/compose/down",
    "/compose/restart",
    "/comfyui/install-node-requirements",
    "/plugins/searxng/enable",
    "/plugins/searxng/disable",
]


@pytest.mark.parametrize("path", CONFIRM_GATED_ROUTES)
@pytest.mark.parametrize("confirm", ["no", "false", "true", 1, "yes", [True], {"ok": True}])
def test_a_truthy_confirm_that_is_not_json_true_is_refused(tmp_path, path, confirm):
    cp, _ = _cp(tmp_path)
    resp = _client(cp).post(path, json={"confirm": confirm})
    assert resp.status_code == 400
    assert "requires confirmation" in resp.json()["error"]
    backend = cp.broker.backend
    acted = (backend.started, backend.stopped, backend.restarted, backend.recreate_calls,
             backend.recreate_batches, backend.container_restart_calls, backend.compose_up_calls,
             backend.compose_down_calls, backend.compose_restart_calls, backend.execs)
    assert all(calls == [] for calls in acted)


@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_a_plugin_change_without_confirm_is_refused_and_writes_nothing(tmp_path, verb):
    cp, src = _cp(tmp_path)
    before = src.read_text(encoding="utf-8")
    resp = _client(cp).post(f"/plugins/searxng/{verb}", json={})
    assert resp.status_code == 400
    assert "requires confirmation" in resp.json()["error"]
    assert src.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_a_plugin_dry_run_names_the_change_and_writes_nothing(tmp_path, verb):
    cp, src = _cp(tmp_path)
    before = src.read_text(encoding="utf-8")
    resp = _client(cp).post(f"/plugins/searxng/{verb}", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json() == {"would": verb, "plugin": "searxng"}
    assert src.read_text(encoding="utf-8") == before
    assert cp.broker.backend.recreate_batches == []


def test_confirm_true_passes_the_gate(tmp_path):
    cp, _ = _cp(tmp_path)
    resp = _client(cp).post("/services/llamacpp/stop", json={"confirm": True})
    assert resp.status_code == 200
    assert cp.broker.backend.stopped == ["llamacpp"]


# --- A JSON body that is not an object ---

@pytest.mark.parametrize("raw", [b"[]", b'[{"confirm": true}]', b'"confirm"', b"42", b"true", b"null"])
def test_a_json_body_that_is_not_an_object_is_a_400(tmp_path, raw):
    cp, _ = _cp(tmp_path)
    client = TestClient(cp.app(auth_token=TOKEN), raise_server_exceptions=False,
                        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    resp = client.post("/services/llamacpp/stop", content=raw)
    assert resp.status_code == 400
    assert resp.json() == {"error": "JSON body must be an object"}
    assert cp.broker.backend.stopped == []
