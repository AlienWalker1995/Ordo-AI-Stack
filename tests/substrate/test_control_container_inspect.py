"""Read-only container views that replace Hermes' `docker inspect` / `docker ps` (SEC-1 step 3).

`GET /containers/{name}` answers an Ordo container's shape: image, state, health, start time,
restart count, mounts, networks and ports. It is built from `docker inspect` through a FIELD
ALLOWLIST (`broker.summarize_inspect`): the environment is never returned (it carries secret
values), and neither are labels (the render puts each file secret's digest into a label, and any
image may label anything). `GET /containers` rows gain the compose project, service and health.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control import broker as broker_module
from ordo.control import principals
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend, summarize_inspect
from ordo.control.scheduler import Scheduler
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
ADMIN_TOKEN = "admin-token-inspect"
HERMES_TOKEN = "hermes-token-inspect"
HERMES = {"Authorization": f"Bearer {HERMES_TOKEN}"}

SECRET = "sk-super-secret-value-0d6e"
# A trimmed real `docker inspect` object, with the things that must never leave: secret env values,
# a secret-file digest label, and a label an image could set to anything.
RAW = {
    "Id": "c0ffee",
    "Name": "/ordo-n8n-1",
    "Image": "sha256:abc123",
    "RestartCount": 2,
    "Config": {
        "Image": "ordo/n8n:1a2b3c4d5e6f",
        "Env": [f"N8N_API_KEY={SECRET}", "TZ=UTC"],
        "Labels": {
            "com.docker.compose.project": "ordo",
            "com.docker.compose.service": "n8n",
            "ordo.secret-files.digest": SECRET,
            "org.opencontainers.image.description": SECRET,
        },
        "Cmd": ["n8n", "--password", SECRET],
        "Entrypoint": ["/entrypoint.sh"],
    },
    "State": {"Status": "running", "Running": True, "StartedAt": "2026-09-29T10:00:00.1Z",
              "ExitCode": 0, "Health": {"Status": "healthy", "Log": [{"Output": SECRET}]}},
    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "Binds": [f"/x:/y:{SECRET}"]},
    "Mounts": [
        {"Type": "bind", "Source": "/c/dev/ordo-ai-stack/out/secrets/n8n_api_key",
         "Destination": "/run/secrets/n8n_api_key", "RW": False},
        {"Type": "volume", "Name": "ordo_n8n-data", "Source": "/var/lib/docker/volumes/ordo_n8n-data/_data",
         "Destination": "/home/node/.n8n", "RW": True},
    ],
    "NetworkSettings": {
        "Networks": {"ordo-net": {"IPAddress": "172.19.0.9"}, "ordo-comfyui-net": {}},
        "Ports": {"5678/tcp": None, "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9000"}]},
    },
}

FIELDS = {"name", "project", "service", "image", "image_id", "state", "health", "started_at",
          "restart_count", "restart_policy", "mounts", "networks", "ports"}


# --------------------------------------------------------------------------- #
# the field allowlist
# --------------------------------------------------------------------------- #

def test_the_summary_has_exactly_the_allowed_fields():
    assert set(summarize_inspect(RAW)) == FIELDS


def test_no_secret_reaches_the_summary():
    text = json.dumps(summarize_inspect(RAW))
    assert SECRET not in text
    for forbidden in ("Env", "env", "Labels", "labels", "Cmd", "Entrypoint", "Binds", "Log"):
        assert f'"{forbidden}"' not in text


def test_the_summary_describes_the_container():
    s = summarize_inspect(RAW)
    assert (s["name"], s["project"], s["service"]) == ("ordo-n8n-1", "ordo", "n8n")
    assert (s["image"], s["image_id"]) == ("ordo/n8n:1a2b3c4d5e6f", "sha256:abc123")
    assert (s["state"], s["health"], s["started_at"]) == ("running", "healthy", "2026-09-29T10:00:00.1Z")
    assert (s["restart_count"], s["restart_policy"]) == (2, "unless-stopped")
    assert s["mounts"] == [
        {"type": "bind", "source": "/c/dev/ordo-ai-stack/out/secrets/n8n_api_key",
         "destination": "/run/secrets/n8n_api_key", "rw": False},
        {"type": "volume", "source": "ordo_n8n-data", "destination": "/home/node/.n8n", "rw": True},
    ]
    assert s["networks"] == ["ordo-comfyui-net", "ordo-net"]
    assert s["ports"] == [{"container": "5678/tcp", "host": None},
                          {"container": "9000/tcp", "host": "127.0.0.1:9000"}]


def test_a_container_without_a_healthcheck_reports_none():
    raw = {**RAW, "State": {"Status": "exited", "StartedAt": "", "ExitCode": 1}}
    s = summarize_inspect(raw)
    assert (s["state"], s["health"]) == ("exited", None)


def test_a_sparse_inspect_object_does_not_raise():
    assert set(summarize_inspect({"Name": "/x"})) == FIELDS


# --------------------------------------------------------------------------- #
# the host-wide list gains project, service and health
# --------------------------------------------------------------------------- #

def test_a_ps_line_becomes_a_container_row():
    line = "nas-stack-janitorr\trunning\tghcr.io/schaka/janitorr:jvm-v2.1.1\tnas-stack\tjanitorr\tUp 3 hours (unhealthy)"
    assert broker_module.DockerBackend.container_row(line) == {
        "name": "nas-stack-janitorr", "status": "running", "image": "ghcr.io/schaka/janitorr:jvm-v2.1.1",
        "project": "nas-stack", "service": "janitorr", "health": "unhealthy",
    }


def test_a_container_outside_compose_has_empty_project_and_service():
    row = broker_module.DockerBackend.container_row("adhoc\texited\talpine\t\t\tExited (0) 2 days ago")
    assert (row["project"], row["service"], row["health"]) == ("", "", None)


def test_a_malformed_ps_line_is_skipped():
    assert broker_module.DockerBackend.container_row("only\ttwo") is None


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #

@pytest.fixture
def cp(tmp_path):
    src = tmp_path / "ordo.yaml"
    src.write_text(yaml.safe_dump(
        {"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto", "plugins": "auto"}
    ))
    scheduler = Scheduler(32)
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out", scheduler=scheduler,
                        broker=Broker(scheduler, MockBackend({"services": {"n8n": {"image": "n8nio/n8n"}}})))


def test_the_route_answers_the_summary(cp):
    cp.broker.backend.inspect_result = summarize_inspect(RAW)
    status, payload = cp.route("GET", "/containers/ordo-n8n-1")
    assert status == 200
    assert payload["name"] == "ordo-n8n-1"
    assert cp.broker.backend.inspect_requests == ["ordo-n8n-1"]


def test_a_container_outside_the_project_is_404(cp):
    def refuse(name):
        raise ValueError(f"container {name!r} is not in project 'ordo'")
    cp.broker.backend.container_inspect = refuse
    status, payload = cp.route("GET", "/containers/nas-stack-janitorr")
    assert status == 404
    assert "not in project" in payload["error"]


def test_the_logs_and_restart_routes_are_not_shadowed(cp):
    assert cp.route("GET", "/containers/ordo-n8n-1/logs")[0] == 200
    assert cp.broker.backend.inspect_requests == []


def test_hermes_may_inspect_an_ordo_container(cp):
    cp.broker.backend.inspect_result = summarize_inspect(RAW)
    client = TestClient(cp.app(auth_token=ADMIN_TOKEN, scoped=[principals.hermes(lambda: HERMES_TOKEN)]))
    r = client.get("/containers/ordo-n8n-1", headers=HERMES)
    assert r.status_code == 200
    assert SECRET not in r.text


def test_the_allowlist_grants_inspect_as_one_segment():
    hermes = principals.hermes(lambda: HERMES_TOKEN)
    assert hermes.allows("GET", "/containers/ordo-n8n-1")
    assert not hermes.allows("GET", "/containers/a/b")
    assert not hermes.allows("POST", "/containers/ordo-n8n-1")
