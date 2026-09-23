"""Hermes' Docker access, checked against the running stack.

The agent container (`agent`) has the host Docker socket and the root group needed to use it:
operator-granted on 2026-08-09, with guardrails in its prompt (docs/design/hermes-owns-docker.md).
The hermes-dashboard container must have neither. Both still reach ops-controller.

These are live checks (`docker exec` / `docker inspect`); they skip when the containers are not up.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

# Container names are "<compose project>-<service>-1"; the service that runs
# the Hermes agent is `agent` (renamed from hermes-gateway). Derive the
# project prefix from COMPOSE_PROJECT_NAME so a project rename doesn't
# silently re-break this suite into a permanent skip.
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "ordo")
GATEWAY = f"{COMPOSE_PROJECT_NAME}-agent-1"
DASHBOARD = f"{COMPOSE_PROJECT_NAME}-hermes-dashboard-1"


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _docker_exec(container: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *cmd],
        capture_output=True, text=True,
    )


def _inspect(container: str) -> dict:
    r = subprocess.run(
        ["docker", "inspect", container],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)[0]


@pytest.fixture(scope="module")
def gateway() -> str:
    if not _container_running(GATEWAY):
        pytest.skip(f"{GATEWAY} not running; bring the stack up to run this suite")
    return GATEWAY


@pytest.fixture(scope="module")
def dashboard() -> str:
    if not _container_running(DASHBOARD):
        pytest.skip(f"{DASHBOARD} not running; bring the stack up to run this suite")
    return DASHBOARD


def test_hermes_agent_has_the_docker_socket(gateway: str):
    """By design (docs/design/hermes-owns-docker.md): Hermes operates the host's Docker."""
    r = _docker_exec(gateway, "test", "-S", "/var/run/docker.sock")
    assert r.returncode == 0, "the agent lost /var/run/docker.sock; Hermes can no longer run Docker"


def test_hermes_dashboard_has_no_docker_sock(dashboard: str):
    r = _docker_exec(dashboard, "test", "-S", "/var/run/docker.sock")
    assert r.returncode != 0, (
        "FAIL: /var/run/docker.sock present in hermes-dashboard"
    )


def test_hermes_agent_is_in_the_root_group(gateway: str):
    """The socket is root:root mode 660 on Docker Desktop; without group 0 every docker call
    from the unprivileged hermes user fails with EACCES."""
    group_add = _inspect(gateway)["HostConfig"].get("GroupAdd", []) or []
    assert "0" in group_add, f"group_add is {group_add!r}; the hermes user cannot use the socket"


def test_hermes_dashboard_not_in_root_group(dashboard: str):
    parsed = _inspect(dashboard)
    group_add = parsed["HostConfig"].get("GroupAdd", []) or []
    assert "0" not in group_add


def test_hermes_can_reach_ops_controller(gateway: str):
    """The whole point of Task 8: Hermes must still be able to call
    ops-controller for privileged verbs. A simple GET /health from inside
    hermes-gateway proves the network path is intact."""
    r = _docker_exec(gateway, "curl", "-fsS", "http://ops-controller:9000/health")
    assert r.returncode == 0, (
        f"FAIL: hermes-gateway cannot reach ops-controller — stderr: {r.stderr!r}"
    )


def test_ops_controller_token_present_in_hermes_env(gateway: str):
    """Hermes must have OPS_CONTROLLER_TOKEN in its env so OpsClient can
    authenticate to ops-controller. The compose `:?` failsafe should make
    this impossible to forget at boot, but verify on the running container."""
    parsed = _inspect(gateway)
    env = parsed["Config"]["Env"]
    has_token = any(e.startswith("OPS_CONTROLLER_TOKEN=") for e in env)
    assert has_token, "FAIL: OPS_CONTROLLER_TOKEN missing from hermes-gateway env"
    has_url = any(e.startswith("OPS_CONTROLLER_URL=") for e in env)
    assert has_url, "FAIL: OPS_CONTROLLER_URL missing from hermes-gateway env"
