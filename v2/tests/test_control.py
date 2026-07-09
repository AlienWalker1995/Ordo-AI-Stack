"""Control plane exposes the substrate over HTTP and switches models drift-safely (one write path)."""
from pathlib import Path

import yaml

from ordo.broker import Broker, MockBackend
from ordo.catalog import Catalog
from ordo.control import ControlPlane
from ordo.plugins import PluginRegistry
from ordo.scheduler import Scheduler

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")


def _cp(tmp_path, model="auto", with_broker=True):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": model, "plugins": "auto"}
    ))
    sched = Scheduler(32)
    broker = Broker(sched, MockBackend()) if with_broker else None
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=sched, broker=broker), src


def test_status_merges_manifest_and_gpu(tmp_path):
    cp, _ = _cp(tmp_path)
    st = cp.route("GET", "/status")[1]
    assert st["manifest"]["ctx_size"] > 0
    assert st["gpu"]["state"] == "idle"           # nothing running yet


def test_get_model_config_lists_catalog(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/model-config")
    assert code == 200
    assert body["source_model"] == "auto"
    assert any(m["id"] == body["active_model"] for m in body["available"])


def test_set_model_writes_source_and_rerenders(tmp_path):
    cp, src = _cp(tmp_path)
    target = CATALOG.models[0].id                  # any real catalog id
    code, body = cp.route("POST", "/model-config", {"model": target})
    assert code == 200 and body["ok"] and body["active_model"] == target
    # ONE write path: the SOURCE changed, and .env was regenerated from it (never hand-edited)
    assert yaml.safe_load(src.read_text())["model"] == target
    env = (tmp_path / "out" / ".env").read_text()
    assert f"LLAMACPP_MODEL={CATALOG.get(target).file}" in env
    # the drift guarantee: the one ctx value is identical across all three consumers
    manifest = (tmp_path / "out" / "manifest.json").read_text()
    assert str(body["ctx_size"]) in manifest


def test_set_unknown_model_is_404_and_writes_nothing(tmp_path):
    cp, src = _cp(tmp_path)
    before = src.read_text()
    code, body = cp.route("POST", "/model-config", {"model": "does-not-exist"})
    assert code == 404 and "available" in body
    assert src.read_text() == before               # rejected → source untouched
    assert not (tmp_path / "out").exists()          # nothing rendered


def test_job_lifecycle_drives_scheduler(tmp_path):
    cp, _ = _cp(tmp_path)
    cp.route("POST", "/jobs", {"id": "reel", "vram_gb": 17, "kind": "media"})
    cp.route("POST", "/jobs", {"id": "chat", "vram_gb": 4, "kind": "chat"})
    st = cp.route("GET", "/status")[1]["gpu"]
    running = {r["id"] for r in st["running"]}
    assert running == {"reel", "chat"}             # co-run: chat slips beside the reel
    _, after = cp.route("POST", "/jobs/complete", {"id": "reel"})
    assert {r["id"] for r in after["running"]} == {"chat"}


def test_bad_job_body_is_400(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("POST", "/jobs", {"id": "x"})   # missing vram_gb
    assert code == 400 and "error" in body


def test_unknown_route_404(tmp_path):
    cp, _ = _cp(tmp_path)
    assert cp.route("GET", "/nope")[0] == 404
