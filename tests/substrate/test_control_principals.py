"""Scoped principals on the ops-controller API (hostile audit SEC-1).

Every caller used to hold the one admin token, and the audit log named callers only by the
`X-Actor` header they chose to send. Hermes, the agent that reads untrusted input (Discord, web
pages, MCP output), therefore held every verb the control plane has. It now gets its own token,
OPS_CONTROLLER_TOKEN_HERMES, which ops-controller maps to the `hermes` principal and an explicit
route allowlist. The admin token is unchanged. Every audit record names the principal the token
proved, beside the self-declared `X-Actor`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control import principals
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
ADMIN_TOKEN = "admin-token-3b9d"
HERMES_TOKEN = "hermes-token-81c4"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}", "X-Actor": "dashboard"}
HERMES = {"Authorization": f"Bearer {HERMES_TOKEN}", "X-Actor": "hermes:cron:68681701c991"}


@pytest.fixture
def plane(tmp_path, monkeypatch):
    audit_path = tmp_path / "data" / "audit.log"
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", audit_path)
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
    ))
    scheduler = Scheduler(32)
    cp = ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler,
                      broker=Broker(scheduler, MockBackend()))
    return cp, audit_path


def _client(cp, hermes_token=lambda: HERMES_TOKEN) -> TestClient:
    return TestClient(cp.app(auth_token=ADMIN_TOKEN, scoped=[principals.hermes(hermes_token)]),
                      raise_server_exceptions=False)


@pytest.fixture
def client(plane):
    cp, _ = plane
    return _client(cp)


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# the allowlist
# --------------------------------------------------------------------------- #

# What Hermes may call: observation, the GPU lease, and per-service recovery of the Ordo project.
ALLOWED = [
    ("GET", "/status"),
    ("GET", "/containers"),
    ("GET", "/containers/ordo-n8n-1/logs"),
    ("GET", "/containers/ordo-n8n-1"),
    ("GET", "/services"),
    ("GET", "/services/n8n/logs"),
    ("GET", "/stats/services"),
    ("GET", "/plugins"),
    ("GET", "/model-config"),
    ("GET", "/gpus"),
    ("GET", "/registry/models"),
    ("GET", "/registry/gpus"),
    ("GET", "/jobs/history"),
    ("GET", "/diagnostics/dstate"),
    ("GET", "/models/download/status"),
    ("GET", "/doctor"),
    ("POST", "/jobs"),
    ("POST", "/jobs/heartbeat"),
    ("POST", "/jobs/complete"),
    ("POST", "/services/n8n/restart"),
    ("POST", "/services/n8n/recreate"),
    ("POST", "/containers/ordo-n8n-1/restart"),
    ("POST", "/plugins/automation/enable"),
    ("POST", "/plugins/automation/disable"),
    ("POST", "/models/download"),
    ("POST", "/model-config"),
]

# What it may not: stack-wide verbs, stop/start, code execution inside a container, the audit log,
# the retired GPU routes, and any path nothing serves (refused before routing, never a 404 probe).
DENIED = [
    ("POST", "/apply"),
    ("POST", "/compose/up"),
    ("POST", "/compose/down"),
    ("POST", "/compose/restart"),
    ("POST", "/services/n8n/stop"),
    ("POST", "/services/n8n/start"),
    ("POST", "/comfyui/install-node-requirements"),
    ("POST", "/gpu/assign"),
    ("POST", "/registry/models/local-chat/assign-gpu"),
    ("GET", "/audit"),
    ("GET", "/no-such-route"),
    ("POST", "/services/a/b/restart"),          # a service id is one path segment
    ("DELETE", "/services/n8n/restart"),        # the method is part of the grant
]


@pytest.mark.parametrize("method, path", ALLOWED)
def test_hermes_may_call_its_allowlist(method, path):
    assert principals.hermes(lambda: HERMES_TOKEN).allows(method, path)


@pytest.mark.parametrize("method, path", DENIED)
def test_hermes_may_not_call_anything_else(method, path):
    assert not principals.hermes(lambda: HERMES_TOKEN).allows(method, path)


@pytest.mark.parametrize("method, path", ALLOWED)
def test_every_allowlisted_route_exists(plane, method, path):
    """The allowlist names routes the control plane serves: a typo would grant nothing and hide."""
    cp, _ = plane
    status, payload = cp.route(method, path, {}, {})
    assert not (status == 404 and str(payload.get("error", "")).startswith("no route")), (method, path)


def test_the_admin_principal_may_call_every_route():
    admin = principals.admin(lambda: ADMIN_TOKEN)
    for method, path in ALLOWED + DENIED:
        assert admin.allows(method, path)


# --------------------------------------------------------------------------- #
# the HTTP binding
# --------------------------------------------------------------------------- #

def test_the_hermes_token_reads_status(client):
    assert client.get("/status", headers=HERMES).status_code == 200


@pytest.mark.parametrize("method, path", DENIED)
def test_the_hermes_token_is_refused_with_403(client, method, path):
    r = client.request(method, path, json={"confirm": True}, headers=HERMES)
    assert r.status_code == 403
    assert "hermes" in r.json()["error"]


def test_a_refused_hermes_call_never_reaches_the_backend(plane, client):
    cp, _ = plane
    assert client.post("/services/n8n/stop", json={"confirm": True}, headers=HERMES).status_code == 403
    assert client.post("/compose/down", json={"confirm": True}, headers=HERMES).status_code == 403
    assert cp.broker.backend.stopped == []


def test_the_admin_token_keeps_every_route(plane, client):
    cp, _ = plane
    assert client.post("/services/n8n/stop", json={"confirm": True}, headers=ADMIN).status_code == 200
    assert client.get("/audit", headers=ADMIN).status_code == 200


def test_the_hermes_token_is_not_an_admin_token_anywhere_else(plane):
    """Only the scoped list is extended: an app built without it knows no Hermes token."""
    cp, _ = plane
    bare = TestClient(cp.app(auth_token=ADMIN_TOKEN))
    assert bare.get("/status", headers=HERMES).status_code == 401


def test_an_empty_hermes_token_grants_nothing(plane):
    """A store without the key materializes an empty file: the principal is off, not open."""
    cp, _ = plane
    client = _client(cp, hermes_token=lambda: "")
    assert client.get("/status", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer"}).status_code == 401
    assert client.get("/status", headers=ADMIN).status_code == 200


def test_a_rotated_hermes_token_takes_effect_without_a_restart(plane, tmp_path):
    cp, _ = plane
    token_file = tmp_path / "ops_controller_token_hermes"
    token_file.write_text(HERMES_TOKEN + "\n", encoding="utf-8")
    client = _client(cp, hermes_token=lambda: token_file.read_text(encoding="utf-8"))
    assert client.get("/status", headers=HERMES).status_code == 200
    token_file.write_text("hermes-rotated-5e21\n", encoding="utf-8")
    assert client.get("/status", headers={"Authorization": "Bearer hermes-rotated-5e21"}).status_code == 200
    assert client.get("/status", headers=HERMES).status_code == 401
    token_file.unlink()                                       # unreadable: keep the last good value
    assert client.get("/status", headers={"Authorization": "Bearer hermes-rotated-5e21"}).status_code == 200


def test_a_hermes_token_equal_to_the_admin_token_is_the_admin(plane):
    """A misconfigured store that copies the admin value is the admin credential, never less."""
    cp, _ = plane
    client = _client(cp, hermes_token=lambda: ADMIN_TOKEN)
    assert client.post("/services/n8n/stop", json={"confirm": True}, headers=ADMIN).status_code == 200


# --------------------------------------------------------------------------- #
# the audit record names the principal
# --------------------------------------------------------------------------- #

def test_an_allowed_hermes_write_is_recorded_under_its_principal(plane, client):
    _, audit_path = plane
    client.post("/services/n8n/restart", json={"confirm": True}, headers=HERMES)
    [rec] = _records(audit_path)
    assert rec["principal"] == "hermes"
    assert rec["caller"] == "hermes:cron:68681701c991"
    assert (rec["action"], rec["target"], rec["status"]) == ("restart", "n8n", 200)


def test_a_hermes_token_cannot_claim_to_be_the_dashboard(plane, client):
    _, audit_path = plane
    client.post("/services/n8n/restart", json={"confirm": True},
                headers={**HERMES, "X-Actor": "dashboard"})
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["caller"]) == ("hermes", "dashboard")


def test_an_admin_write_is_recorded_under_the_admin_principal(plane, client):
    _, audit_path = plane
    client.post("/services/n8n/restart", json={"confirm": True}, headers=ADMIN)
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["caller"]) == ("admin", "dashboard")


def test_a_refused_hermes_read_is_recorded(plane, client):
    """A denied read is a security signal, not polling noise: it is recorded like a write."""
    _, audit_path = plane
    assert client.get("/audit", headers=HERMES).status_code == 403
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["method"], rec["path"], rec["status"], rec["result"]) == (
        "hermes", "GET", "/audit", 403, "refused")


def test_a_refused_hermes_write_is_recorded_without_its_body(plane, client):
    _, audit_path = plane
    client.post("/compose/down", json={"confirm": True, "service": "n8n", "password": "sekrit"}, headers=HERMES)
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["action"], rec["status"]) == ("hermes", "compose.down", 403)
    assert "sekrit" not in json.dumps(rec)
    assert HERMES_TOKEN not in audit_path.read_text(encoding="utf-8")


def test_an_allowed_hermes_read_is_not_recorded(plane, client):
    _, audit_path = plane
    client.get("/status", headers=HERMES)
    assert _records(audit_path) == []


def test_an_unauthenticated_write_is_recorded_as_unauthenticated(plane, client):
    _, audit_path = plane
    client.post("/services/n8n/stop", json={"confirm": True}, headers={"X-Actor": "hermes"})
    [rec] = _records(audit_path)
    assert (rec["principal"], rec["caller"], rec["status"]) == ("unauthenticated", "hermes", 401)


# --------------------------------------------------------------------------- #
# delivery: the store mints it, the render hands it to ops-controller only
# --------------------------------------------------------------------------- #

def _render_compose() -> dict:
    from ordo.render.config import Source
    from ordo.render.engine import render
    site = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net"}
    rc = render(Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                                  "plugins": "auto", "site": site}), CATALOG, REGISTRY)
    return rc, rc.compose_dict()


def test_ops_controller_reads_the_hermes_token_from_a_file():
    _, c = _render_compose()
    env = c["services"]["ops-controller"]["environment"]
    assert env["OPS_CONTROLLER_TOKEN_HERMES_FILE"] == "/run/secrets/ops_controller_token_hermes"
    assert "OPS_CONTROLLER_TOKEN_HERMES" not in env


def test_no_other_service_is_handed_the_hermes_token_yet():
    """This change is server side only: the agent switches to the token in its own change."""
    _, c = _render_compose()
    holders = sorted(name for name, svc in c["services"].items()
                     if "OPS_CONTROLLER_TOKEN_HERMES" in json.dumps(svc))
    assert holders == ["ops-controller"]


def test_the_hermes_token_is_an_optional_core_secret():
    """Optional until the agent reads it: a store without it must not block `ordo apply`."""
    rc, _ = _render_compose()
    assert "OPS_CONTROLLER_TOKEN_HERMES" in rc.required_secrets
    assert "OPS_CONTROLLER_TOKEN_HERMES" in rc.optional_secrets


def test_the_store_mints_and_rotates_the_hermes_token():
    from ordo.host import secret_store
    value = secret_store.generator_for("OPS_CONTROLLER_TOKEN_HERMES")()
    assert len(value) >= 40
    assert secret_store.is_internal("OPS_CONTROLLER_TOKEN_HERMES")
    assert secret_store.rotation_refusal("OPS_CONTROLLER_TOKEN_HERMES") is None


# --------------------------------------------------------------------------- #
# revocation (security review of #298, finding F1)
# --------------------------------------------------------------------------- #

def test_emptying_the_hermes_token_file_revokes_it_at_once(plane, tmp_path):
    """An empty file is how an operator turns the principal off (a store without the key
    materializes one). It must stop matching on the next request, not keep the old value."""
    cp, _ = plane
    token_file = tmp_path / "ops_controller_token_hermes"
    token_file.write_text(HERMES_TOKEN, encoding="utf-8")
    client = _client(cp, hermes_token=lambda: token_file.read_text(encoding="utf-8"))
    assert client.get("/status", headers=HERMES).status_code == 200
    token_file.write_text("", encoding="utf-8")
    assert client.get("/status", headers=HERMES).status_code == 401
    assert client.get("/status", headers=ADMIN).status_code == 200


def test_a_revoked_hermes_token_can_be_turned_back_on(plane, tmp_path):
    cp, _ = plane
    token_file = tmp_path / "ops_controller_token_hermes"
    token_file.write_text("", encoding="utf-8")
    client = _client(cp, hermes_token=lambda: token_file.read_text(encoding="utf-8"))
    assert client.get("/status", headers=HERMES).status_code == 401
    token_file.write_text(HERMES_TOKEN, encoding="utf-8")
    assert client.get("/status", headers=HERMES).status_code == 200


def test_an_unreadable_hermes_token_file_keeps_the_last_good_value(plane, tmp_path):
    """A read ERROR (a file mid-replace, a transient mount hiccup) is not a revocation."""
    cp, _ = plane
    token_file = tmp_path / "ops_controller_token_hermes"
    token_file.write_text(HERMES_TOKEN, encoding="utf-8")
    client = _client(cp, hermes_token=lambda: token_file.read_text(encoding="utf-8"))
    assert client.get("/status", headers=HERMES).status_code == 200
    token_file.unlink()
    assert client.get("/status", headers=HERMES).status_code == 200


def test_the_admin_token_still_survives_a_torn_empty_read():
    """Unchanged for the admin: an empty read keeps the last good value, so a torn write can
    never lock every caller out (#290). Only scoped principals treat empty as off."""
    values = iter(["admin-token", ""])
    admin = principals.admin(lambda: next(values))
    assert admin.token.current() == "admin-token"
    assert admin.token.current() == "admin-token"
