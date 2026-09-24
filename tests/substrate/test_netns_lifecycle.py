"""Every ops-controller verb that cycles a netns owner also cycles its members.

A service declared `network_mode: service:<owner>` lives in the owner's network namespace. Any verb
that gives the owner a new namespace (restart, stop+start, a `--no-deps` recreate, a named down)
leaves the member attached to the dead one: it keeps running, reports healthy, and has only `lo`.
Observed live 2026-09-24: `POST /services/caddy/restart` left hermes-dashboard and all eight
tailnet sidecars without a network interface. Reproduced in a throwaway compose project:

    bare `docker restart owner`                 -> member: lo            (orphaned)
    then `docker restart member`                -> member: eth0 lo       (repaired)
    `up -d --no-deps --force-recreate owner`    -> member: lo            (orphaned)
    `... --force-recreate owner member`         -> member: eth0 lo
    `docker stop owner` + `docker start owner`  -> member still running, lo (a `docker start` of a
                                                   running member is a no-op, so start must restart it)
    `compose down owner`                        -> member left running, orphaned

The members are derived from the rendered compose by `bringup.lifecycle_group`, the same function
the host's `ordo up` / `ordo recreate` plan with, so the control plane and the host cannot disagree
about who follows whom.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from ordo import bringup
from ordo.broker import Broker, DockerBackend, MockBackend
from ordo.control import ControlPlane
from ordo.scheduler import Job, Scheduler

CONFIRM = {"confirm": True}
MEMBERS = ["hermes-dashboard", "tailnet-chat"]

COMPOSE = {
    "services": {
        "llamacpp": {"image": "x"},
        "oauth2-proxy": {"image": "x", "profiles": ["edge"]},
        "caddy": {"image": "x", "profiles": ["edge"], "depends_on": ["oauth2-proxy"]},
        "tailnet-chat": {"image": "x", "profiles": ["edge"], "network_mode": "service:caddy"},
        "hermes-dashboard": {"image": "x", "profiles": ["hermes-ui"], "network_mode": "service:caddy"},
        "vpn": {"image": "x"},
        "torrent": {"image": "x", "network_mode": "service:vpn"},
        # A GPU resident inside an owner's netns: the lease guard must see it through the owner.
        "gpu-sidecar": {"image": "x", "network_mode": "service:vpn"},
        "open-webui": {"image": "x", "network_mode": "host"},
    }
}


# --- the one place that knows a service's members ---


def test_lifecycle_group_is_the_owner_then_its_members_sorted():
    assert bringup.lifecycle_group(COMPOSE, "caddy") == ["caddy", *MEMBERS]


def test_lifecycle_group_derives_any_owner_not_just_caddy():
    assert bringup.lifecycle_group(COMPOSE, "vpn") == ["vpn", "gpu-sidecar", "torrent"]


@pytest.mark.parametrize("service", ["tailnet-chat", "llamacpp", "open-webui", "not-rendered"])
def test_a_member_or_a_plain_service_is_its_own_group(service):
    # Acting on a member acts on the member only; `network_mode: host` is not a netns owner.
    assert bringup.lifecycle_group(COMPOSE, service) == [service]


def test_plan_named_expands_every_named_owner():
    args, starts = bringup.plan_named(COMPOSE, ["vpn", "caddy"], force_recreate=True)
    assert args == ["up", "-d", "--no-deps", "--force-recreate",
                    "vpn", "gpu-sidecar", "torrent", "caddy", *MEMBERS]
    assert starts == {"vpn", "gpu-sidecar", "torrent", "caddy", *MEMBERS}


def test_plan_named_does_not_repeat_a_member_that_was_also_named():
    args, _ = bringup.plan_named(COMPOSE, ["tailnet-chat", "caddy"], force_recreate=False)
    assert args == ["up", "-d", "--no-deps", "tailnet-chat", "caddy", "hermes-dashboard"]


# --- the control plane's per-container verbs (MockBackend) ---


@pytest.fixture
def backend() -> MockBackend:
    b = MockBackend()
    b.compose_doc = COMPOSE
    return b


@pytest.fixture
def broker(backend) -> Broker:
    return Broker(Scheduler(total_vram_gb=32.0), backend)


@pytest.fixture
def control_plane(tmp_path, broker) -> ControlPlane:
    return ControlPlane(source_path=tmp_path / "ordo.yaml", catalog=MagicMock(), registry=MagicMock(),
                        out_dir=tmp_path, scheduler=broker.scheduler, broker=broker)


def test_restarting_the_owner_restarts_its_members_after_it(control_plane, backend):
    status, body = control_plane.route("POST", "/services/caddy/restart", CONFIRM)
    assert status == 200, body
    assert backend.restarted == ["caddy", *MEMBERS]
    assert body["members"] == MEMBERS


def test_restarting_a_member_restarts_only_the_member(control_plane, backend):
    status, _ = control_plane.route("POST", "/services/tailnet-chat/restart", CONFIRM)
    assert status == 200
    assert backend.restarted == ["tailnet-chat"]


def test_a_plain_service_restart_is_unchanged(control_plane, backend):
    status, body = control_plane.route("POST", "/services/llamacpp/restart", CONFIRM)
    assert status == 200
    assert backend.restarted == ["llamacpp"]
    assert "members" not in body


def test_starting_the_owner_restarts_its_members_after_it(control_plane, backend):
    # Restart, not start: a member left running after the owner stopped is still in the dead
    # namespace, and `docker start` of a running container does nothing.
    status, _ = control_plane.route("POST", "/services/caddy/start", CONFIRM)
    assert status == 200
    assert backend.started == ["caddy"]
    assert backend.restarted == MEMBERS


def test_stopping_the_owner_stops_its_members_first(control_plane, backend):
    status, _ = control_plane.route("POST", "/services/caddy/stop", CONFIRM)
    assert status == 200
    assert backend.stopped == [*MEMBERS, "caddy"]


def test_restarting_the_owner_container_restarts_its_members(control_plane, backend):
    status, _ = control_plane.route("POST", "/containers/ordo-caddy-1/restart", CONFIRM)
    assert status == 200
    assert backend.container_restart_calls == ["ordo-caddy-1"]
    assert backend.restarted == MEMBERS


def test_restarting_a_member_container_restarts_only_it(control_plane, backend):
    status, _ = control_plane.route("POST", "/containers/ordo-tailnet-chat-1/restart", CONFIRM)
    assert status == 200
    assert backend.container_restart_calls == ["ordo-tailnet-chat-1"]
    assert backend.restarted == []


def test_a_failed_member_restart_is_reported(control_plane, backend):
    def fail_on_member(service):
        if service == "tailnet-chat":
            raise RuntimeError("boom")
        backend.restarted.append(service)

    backend.restart = fail_on_member
    status, body = control_plane.route("POST", "/services/caddy/restart", CONFIRM)
    assert status == 500
    assert "tailnet-chat" in body["error"]


def test_an_unreadable_render_refuses_instead_of_orphaning(control_plane, backend):
    def unreadable():
        raise OSError("no /config/docker-compose.yml")

    backend.rendered_compose = unreadable
    status, body = control_plane.route("POST", "/services/caddy/restart", CONFIRM)
    assert status == 500
    assert "docker-compose.yml" in body["error"]
    assert backend.restarted == []


# --- the lease guard sees the whole group ---


@pytest.fixture
def leased(broker, backend):
    """A render holds the card and gpu-sidecar (a member of vpn) was evicted for it."""
    broker.scheduler.cache_idle("gpu-sidecar", 25)
    broker.request(Job("gate-comfyui", 20, "media"))
    assert "gpu-sidecar" in broker.scheduler.evicted_residents
    backend.started.clear()
    backend.stopped.clear()
    return broker


@pytest.mark.parametrize("verb", ["start", "restart", "recreate"])
def test_cycling_an_owner_whose_member_is_evicted_is_refused(control_plane, backend, leased, verb):
    status, body = control_plane.route("POST", f"/services/vpn/{verb}", CONFIRM)
    assert status == 409, body
    assert "gpu-sidecar" in body["error"]
    assert backend.started == [] and backend.restarted == [] and backend.recreate_calls == []


@pytest.mark.parametrize("path,calls", [("/compose/up", "compose_up_calls"),
                                        ("/compose/restart", "compose_restart_calls")])
def test_a_named_compose_verb_on_that_owner_is_refused(control_plane, backend, leased, path, calls):
    status, _ = control_plane.route("POST", path, {"confirm": True, "service": "vpn"})
    assert status == 409
    assert getattr(backend, calls) == []


def test_the_owner_container_is_refused_when_a_member_is_evicted(control_plane, backend, leased):
    status, _ = control_plane.route("POST", "/containers/ordo-vpn-1/restart", CONFIRM)
    assert status == 409
    assert backend.container_restart_calls == []


def test_caddy_is_still_cyclable_during_a_lease(control_plane, backend, leased):
    status, _ = control_plane.route("POST", "/services/caddy/restart", CONFIRM)
    assert status == 200
    assert backend.restarted == ["caddy", *MEMBERS]


# --- DockerBackend's compose verbs: one compose call naming the whole group ---


@pytest.fixture
def docker_backend(tmp_path, monkeypatch) -> tuple[DockerBackend, list[list[str]]]:
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE), encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("ordo.broker.subprocess.run", fake_run)
    b = DockerBackend("ordo")
    b.COMPOSE_DIR = Path(tmp_path).as_posix()
    return b, calls


def _tail(cmd: list[str], verb: str) -> list[str]:
    return cmd[cmd.index(verb):]


def test_recreating_the_owner_is_one_no_deps_call_naming_the_members(docker_backend):
    backend, calls = docker_backend
    backend.recreate_service("caddy")
    assert len(calls) == 1
    assert _tail(calls[0], "up") == ["up", "-d", "--no-deps", "--force-recreate", "caddy", *MEMBERS]
    assert calls[0] == backend._compose(*_tail(calls[0], "up"), all_profiles=True)


def test_a_named_compose_up_uses_the_host_planner(docker_backend, tmp_path):
    backend, calls = docker_backend
    backend.compose_up("caddy")
    args, _ = bringup.plan_named(COMPOSE, ["caddy"], force_recreate=False)
    # Same argv the host's `ordo up caddy` builds: the shared planner, every profile.
    assert calls == [bringup.compose_argv(backend.COMPOSE_DIR, "ordo", *args,
                                          profiles=bringup.profiles_in(COMPOSE))]


def test_a_whole_stack_compose_up_is_unchanged(docker_backend):
    backend, calls = docker_backend
    backend.compose_up(None)
    assert _tail(calls[0], "up") == ["up", "-d"]


@pytest.mark.parametrize("verb,method", [("restart", "compose_restart"), ("down", "compose_down")])
def test_named_compose_restart_and_down_name_the_members(docker_backend, verb, method):
    backend, calls = docker_backend
    getattr(backend, method)("caddy")
    assert _tail(calls[0], verb) == [verb, "caddy", *MEMBERS]


def test_a_member_compose_verb_names_only_the_member(docker_backend):
    backend, calls = docker_backend
    backend.recreate_service("tailnet-chat")
    assert _tail(calls[0], "up") == ["up", "-d", "--no-deps", "--force-recreate", "tailnet-chat"]


def test_the_backend_reads_the_render_it_acts_on(docker_backend):
    backend, _ = docker_backend
    assert backend.rendered_compose() == COMPOSE
