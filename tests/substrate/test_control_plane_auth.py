"""Bearer-token authentication on the ops-controller HTTP API.

The control plane can stop services, re-render the stack and hand out GPU leases, so every call
must prove it holds OPS_CONTROLLER_TOKEN. Only the health probe is open (container healthchecks
carry no credentials). Enforcement lives in the HTTP layer; route() stays a pure function.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
TOKEN = "test-token-7f3c"


def _cp(tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
    ))
    sched = Scheduler(32)
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=sched,
                        broker=Broker(sched, MockBackend()))


@pytest.fixture
def client(tmp_path):
    return TestClient(_cp(tmp_path).app(auth_token=TOKEN))


def test_a_request_without_a_token_is_refused(client):
    r = client.get("/status")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", [
    "Bearer wrong-token",
    f"Bearer {TOKEN}x",
    f"Token {TOKEN}",
    "Bearer",
    "Bearer ",
    TOKEN,
])
def test_a_wrong_or_malformed_token_is_refused(client, header):
    assert client.get("/status", headers={"Authorization": header}).status_code == 401


def test_the_right_token_is_accepted(client):
    r = client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert "gpu" in r.json()


def test_the_scheme_is_case_insensitive(client):
    assert client.get("/status", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_the_health_probe_needs_no_token(client, path):
    assert client.get(path).status_code == 200


def test_a_refused_write_changes_nothing(tmp_path):
    cp = _cp(tmp_path)
    client = TestClient(cp.app(auth_token=TOKEN))
    r = client.post("/jobs", json={"id": "sneaky", "kind": "media", "vram_gb": 4})
    assert r.status_code == 401
    assert not cp.scheduler.status()["running"] and not cp.scheduler.status()["queued"]


def test_the_app_refuses_to_start_without_a_token(tmp_path):
    cp = _cp(tmp_path)
    for missing in ("", "   ", None):
        with pytest.raises(ValueError):
            cp.app(auth_token=missing)


def test_a_refusal_is_logged_without_the_presented_token(client, caplog):
    with caplog.at_level(logging.WARNING, logger="ordo.control.api"):
        client.get("/status", headers={"Authorization": "Bearer leaked-guess-123"})
    text = caplog.text
    assert "401" in text or "refused" in text
    assert "leaked-guess-123" not in text and TOKEN not in text


# --------------------------------------------------------------------------- #
# The token is a file (/run/secrets): a rotation rewrites it, and the running API takes the new
# value on its next request. Holding the startup value refused the host's own lease probe (which
# reads the same file) until a recreate the lease check itself blocked.
# --------------------------------------------------------------------------- #


def test_a_rotated_token_file_takes_effect_without_a_restart(tmp_path):
    token_file = tmp_path / "ops_controller_token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    client = TestClient(_cp(tmp_path).app(auth_token=lambda: token_file.read_text(encoding="utf-8")))
    assert client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    token_file.write_text("rotated-token-91ab\n", encoding="utf-8")
    assert client.get("/status", headers={"Authorization": "Bearer rotated-token-91ab"}).status_code == 200
    assert client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_an_empty_or_unreadable_token_file_keeps_the_last_good_token(tmp_path):
    token_file = tmp_path / "ops_controller_token"
    token_file.write_text(TOKEN, encoding="utf-8")
    client = TestClient(_cp(tmp_path).app(auth_token=lambda: token_file.read_text(encoding="utf-8")))
    token_file.write_text("", encoding="utf-8")              # a torn write
    assert client.get("/status").status_code == 401           # never open
    assert client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    token_file.unlink()
    assert client.get("/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_a_token_source_that_is_empty_at_start_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _cp(tmp_path).app(auth_token=lambda: "")
