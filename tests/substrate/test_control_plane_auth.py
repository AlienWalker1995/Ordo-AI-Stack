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
