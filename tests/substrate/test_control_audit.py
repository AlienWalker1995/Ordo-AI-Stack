"""The ops-controller audit log records every state-changing call, in one place.

The contract (docs/data.md, "Audit log"): each POST to the control plane, whatever it does and
whether it succeeds, is refused (401, 409, ...) or fails, leaves exactly one JSONL record naming
who asked (`X-Actor`), what they asked for, and how it ended. Reads leave none. The record never
carries a credential or any request body field beyond the few it names. The file is bounded: it
rotates by size and keeps a fixed number of generations.

These tests drive the real HTTP binding (`ControlPlane.app`), because that is where the actor,
the 401 refusal and the dispatch all meet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control.api import ControlPlane
from ordo.control.audit import AuditLog
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
TOKEN = "audit-test-token-5d1e"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Actor": "dashboard"}


@pytest.fixture
def plane(tmp_path, monkeypatch):
    audit_path = tmp_path / "data" / "audit.log"
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr("ordo.control.api.COMFYUI_CUSTOM_NODES_DIR", tmp_path / "custom_nodes")
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
    ))
    scheduler = Scheduler(32)
    cp = ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler,
                      broker=Broker(scheduler, MockBackend()))
    return cp, audit_path


@pytest.fixture
def client(plane):
    cp, _ = plane
    return TestClient(cp.app(auth_token=TOKEN), raise_server_exceptions=False)


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


PRIVILEGED_CALLS = [
    # (path, body, action, target)
    ("/services/llamacpp/start", {"confirm": True}, "start", "llamacpp"),
    ("/services/llamacpp/stop", {"confirm": True}, "stop", "llamacpp"),
    ("/services/llamacpp/restart", {"confirm": True}, "restart", "llamacpp"),
    ("/services/llamacpp/recreate", {"confirm": True}, "recreate", "llamacpp"),
    ("/containers/ordo-llamacpp-1/restart", {"confirm": True}, "container.restart", "ordo-llamacpp-1"),
    ("/compose/up", {"confirm": True, "service": "open-webui"}, "compose.up", "open-webui"),
    ("/compose/down", {"confirm": True, "service": "open-webui"}, "compose.down", "open-webui"),
    ("/compose/restart", {"confirm": True, "service": "open-webui"}, "compose.restart", "open-webui"),
    ("/model-config", {"model": "no-such-model"}, "model_config", "no-such-model"),
    ("/plugins/no-such-plugin/enable", {}, "plugin.enable", "no-such-plugin"),
    ("/plugins/no-such-plugin/disable", {}, "plugin.disable", "no-such-plugin"),
    ("/jobs", {"id": "render-1", "vram_gb": 4, "kind": "media"}, "lease.request", "render-1"),
    ("/jobs/complete", {"id": "render-1"}, "lease.release", "render-1"),
    ("/jobs/heartbeat", {"id": "render-1"}, "lease.heartbeat", "render-1"),
    ("/models/download", {"url": "http://example.invalid/w.safetensors"}, "models.download", "w.safetensors"),
    ("/comfyui/install-node-requirements", {"node_path": "Pack"}, "comfyui_pip_install", "Pack"),
    ("/gpu/assign", {"service": "llamacpp"}, "gpu_assign", "llamacpp"),
    ("/registry/models/local-chat/assign-gpu", {}, "gpu_assign", "local-chat"),
]


@pytest.mark.parametrize("path, body, action, target", PRIVILEGED_CALLS)
def test_each_privileged_call_leaves_exactly_one_record(plane, client, path, body, action, target):
    _, audit_path = plane
    r = client.post(path, json=body, headers=AUTH)

    records = _records(audit_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["action"] == action
    assert rec["target"] == target
    assert rec["caller"] == "dashboard"
    assert rec["method"] == "POST"
    assert rec["path"] == path
    assert rec["status"] == r.status_code
    assert rec["confirm"] is bool(body.get("confirm"))
    assert rec["dry_run"] is False
    assert isinstance(rec["ts"], float)
    expected_result = "ok" if r.status_code < 400 else ("refused" if r.status_code < 500 else "error")
    assert rec["result"] == expected_result
    if r.status_code >= 400:
        assert rec["error"]


def test_a_dry_run_is_recorded_as_one(plane, client):
    _, audit_path = plane
    r = client.post("/services/llamacpp/stop", json={"dry_run": True}, headers=AUTH)
    assert r.status_code == 200
    [rec] = _records(audit_path)
    assert (rec["action"], rec["dry_run"], rec["confirm"], rec["result"]) == ("stop", True, False, "ok")


def test_a_lease_grant_is_recorded_as_granted(plane, client):
    _, audit_path = plane
    client.post("/jobs", json={"id": "render-1", "vram_gb": 4, "kind": "media"}, headers=AUTH)
    [rec] = _records(audit_path)
    assert rec["detail"] == "granted"


def test_a_lease_the_card_can_never_hold_is_recorded_as_rejected(plane, client):
    _, audit_path = plane
    client.post("/jobs", json={"id": "huge", "vram_gb": 64, "kind": "media"}, headers=AUTH)
    [rec] = _records(audit_path)
    assert rec["detail"] == "rejected"


def test_a_lease_refusal_409_is_recorded(plane, client):
    cp, audit_path = plane
    cp.scheduler.cache_idle("llamacpp", 25)
    # A media job that does not fit beside the resident evicts it for the lease...
    client.post("/jobs", json={"id": "render-1", "vram_gb": 18, "kind": "media"}, headers=AUTH)
    assert cp.scheduler.evicted_residents
    # ...so starting the resident again is refused with 409, and that refusal is recorded too.
    r = client.post("/services/llamacpp/start", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 409

    grant, refusal = _records(audit_path)
    assert (grant["action"], grant["status"], grant["detail"]) == ("lease.request", 200, "granted")
    assert (refusal["action"], refusal["status"], refusal["result"]) == ("start", 409, "refused")
    assert "evicted" in refusal["error"]


def test_an_unauthenticated_write_is_refused_and_recorded(plane, client):
    cp, audit_path = plane
    r = client.post("/services/llamacpp/stop", json={"confirm": True}, headers={"X-Actor": "someone"})
    assert r.status_code == 401
    assert cp.broker.backend.stopped == []

    [rec] = _records(audit_path)
    assert (rec["action"], rec["target"], rec["status"], rec["result"]) == ("stop", "llamacpp", 401, "refused")
    # The actor header of an unauthenticated call is only a claim, but it is still what was sent.
    assert rec["caller"] == "someone"


def test_an_unauthenticated_read_is_not_recorded(plane, client):
    _, audit_path = plane
    assert client.get("/status").status_code == 401
    assert _records(audit_path) == []


def test_a_call_without_an_actor_header_is_recorded_as_unknown(plane, client):
    _, audit_path = plane
    client.post("/services/llamacpp/stop", json={"dry_run": True},
                headers={"Authorization": f"Bearer {TOKEN}"})
    assert _records(audit_path)[0]["caller"] == "unknown"


def test_the_actor_header_is_sanitised_and_bounded(plane, client):
    _, audit_path = plane
    client.post("/services/llamacpp/stop", json={"dry_run": True},
                headers={"Authorization": f"Bearer {TOKEN}", "X-Actor": "hermes\"}{evil" + "x" * 500})
    caller = _records(audit_path)[0]["caller"]
    assert caller.startswith("hermes")
    assert len(caller) <= 64
    assert '"' not in caller and "{" not in caller


def test_an_invalid_json_body_is_recorded(plane, client):
    _, audit_path = plane
    r = client.post("/services/llamacpp/stop", content=b"{not json", headers=AUTH)
    assert r.status_code == 400
    [rec] = _records(audit_path)
    assert (rec["action"], rec["status"], rec["result"]) == ("stop", 400, "refused")


def test_an_unknown_write_route_is_recorded(plane, client):
    _, audit_path = plane
    assert client.post("/nope", json={}, headers=AUTH).status_code == 404
    [rec] = _records(audit_path)
    assert (rec["action"], rec["status"], rec["result"]) == ("unknown", 404, "refused")


def test_a_handler_that_raises_is_recorded_as_an_error(plane, client, monkeypatch):
    cp, audit_path = plane

    def boom(*_args, **_kwargs):
        raise RuntimeError("backend exploded")
    monkeypatch.setattr(cp, "service_stop", boom)
    assert client.post("/services/llamacpp/stop", json={"confirm": True}, headers=AUTH).status_code == 500
    [rec] = _records(audit_path)
    assert (rec["action"], rec["status"], rec["result"]) == ("stop", 500, "error")
    assert "backend exploded" in rec["error"]


@pytest.mark.parametrize("path", [
    "/status", "/model-config", "/plugins", "/services", "/containers", "/stats/services",
    "/jobs/history", "/registry/models", "/models/download/status", "/audit", "/health",
    "/services/llamacpp/logs",
])
def test_reads_leave_no_record(plane, client, path, monkeypatch):
    cp, audit_path = plane
    monkeypatch.setattr(cp, "_live_gpus", lambda: {})
    client.get(path, headers=AUTH)
    assert _records(audit_path) == []


def test_no_secret_or_body_field_beyond_the_named_ones_is_recorded(plane, client):
    _, audit_path = plane
    client.post(
        "/models/download",
        json={"url": "https://huggingface.co/x/resolve/main/w.safetensors?token=hf_sekrit_query",
              "filename": "", "api_key": "sk-sekrit-body", "confirm": True},
        headers={**AUTH, "X-Request-ID": "r-1"},
    )
    client.post("/services/llamacpp/stop", json={"confirm": True, "password": "sekrit-pw"},
                headers={"Authorization": "Bearer wrong-sekrit-token"})
    raw = audit_path.read_text(encoding="utf-8")
    for secret in (TOKEN, "wrong-sekrit-token", "hf_sekrit_query", "sk-sekrit-body", "sekrit-pw", "token="):
        assert secret not in raw
    allowed = {"ts", "caller", "action", "target", "result", "method", "path", "status",
               "dry_run", "confirm", "error", "detail"}
    for rec in _records(audit_path):
        assert set(rec) <= allowed


def test_long_error_messages_are_truncated(plane, client, monkeypatch):
    cp, audit_path = plane
    monkeypatch.setattr(cp, "service_stop", lambda *_a: cp._error(500, "x" * 5000))
    client.post("/services/llamacpp/stop", json={"confirm": True}, headers=AUTH)
    assert len(_records(audit_path)[0]["error"]) <= 300


# --- GET /audit ---

def test_get_audit_returns_the_newest_records_first_and_needs_the_bearer(plane, client):
    for i in range(3):
        client.post(f"/services/s{i}/stop", json={"dry_run": True}, headers=AUTH)
    assert client.get("/audit?limit=2").status_code == 401
    r = client.get("/audit?limit=2", headers=AUTH)
    assert r.status_code == 200
    assert [e["target"] for e in r.json()["entries"]] == ["s2", "s1"]


def test_get_audit_bounds_the_limit(plane, client):
    assert client.get("/audit?limit=0", headers=AUTH).status_code == 422
    assert client.get("/audit?limit=100000", headers=AUTH).status_code == 422


# --- rotation ---

def test_the_log_rotates_by_size_and_keeps_a_fixed_number_of_generations(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=500, backups=2)
    for i in range(100):
        log.record(action="stop", target=f"s{i}", result="ok", caller="t")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["audit.1.log", "audit.2.log", "audit.log"]
    for p in tmp_path.iterdir():
        # A file only rotates once it reaches max_bytes, so it can exceed it by at most one line.
        assert p.stat().st_size < 500 + 200
    # Nothing is lost inside the kept window: the generations are contiguous, oldest in .2.
    targets = [json.loads(line)["target"] for name in ("audit.2.log", "audit.1.log", "audit.log")
               for line in (tmp_path / name).read_text(encoding="utf-8").splitlines()]
    numbers = [int(t[1:]) for t in targets]
    assert numbers == list(range(numbers[0], 100))


def test_tail_reads_across_rotated_generations_newest_first(tmp_path):
    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=500, backups=3)
    for i in range(20):
        log.record(action="stop", target=f"s{i}", result="ok", caller="t")
    assert (tmp_path / "audit.1.log").exists()
    assert [e["target"] for e in log.tail(12)] == [f"s{i}" for i in range(19, 7, -1)]


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert AuditLog(tmp_path / "none" / "audit.log").tail(10) == []
    assert not (tmp_path / "none").exists()      # reading never creates the directory


def test_existing_records_stay_readable(tmp_path):
    # Records written before this format (no method/path/status) still read back unchanged.
    path = tmp_path / "audit.log"
    old = {"ts": 1790141506.08, "caller": "dashboard", "action": "pull", "target": "x", "result": "ok"}
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    log = AuditLog(path)
    log.record(action="stop", target="y", result="ok", caller="t")
    assert log.tail(10)[1] == old
