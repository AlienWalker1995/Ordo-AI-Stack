"""Hermes' Docker access, checked against the running stack.

The agent container (`agent`) has the host Docker socket, and its unprivileged hermes user can use
it: operator-granted on 2026-08-09, with guardrails in its prompt (docs/design/hermes-owns-docker.md).
The Hermes dashboard (web UI and desktop apps) runs inside the same container, as upstream's s6
`dashboard` service next to the gateways, so its sessions have the same access. It reaches ops-controller.

These are live checks (`docker exec` / `docker inspect`); they skip when the containers are not up.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

# Live checks: run by CI's docker job against a real bring-up, skipped where nothing is running.
pytestmark = pytest.mark.docker

# Container names are "<compose project>-<service>-1"; the service that runs
# the Hermes agent is `agent` (renamed from hermes-gateway). Derive the
# project prefix from COMPOSE_PROJECT_NAME so a project rename doesn't
# silently re-break this suite into a permanent skip.
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "ordo")
GATEWAY = f"{COMPOSE_PROJECT_NAME}-agent-1"


def _container_running(name: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True,
        )
    except OSError:  # no docker CLI on this machine: nothing is running
        return False
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


def test_hermes_agent_has_the_docker_socket(gateway: str):
    """By design (docs/design/hermes-owns-docker.md): Hermes operates the host's Docker."""
    r = _docker_exec(gateway, "test", "-S", "/var/run/docker.sock")
    assert r.returncode == 0, "the agent lost /var/run/docker.sock; Hermes can no longer run Docker"


def test_hermes_user_can_use_the_docker_socket(gateway: str):
    """The socket is root:root mode 660 on Docker Desktop. The image's boot (upstream's s6 stage2)
    adds the hermes user to the socket's group, and the gateway runs as that user, so the check is
    a real Docker call made as hermes, the way the gateway makes it."""
    r = _docker_exec(gateway, "/command/s6-setuidgid", "hermes",
                     "docker", "version", "--format", "{{.Server.Version}}")
    assert r.returncode == 0 and r.stdout.strip(), (
        f"the hermes user cannot use /var/run/docker.sock: {r.stderr.strip()!r}")


def test_the_dashboard_runs_beside_the_gateways(gateway: str):
    """With the dashboard enabled it is the s6 `dashboard` service in this container: up, answering,
    and reporting the gateway as running (what the desktop apps show)."""
    env = dict(e.split("=", 1) for e in _inspect(gateway)["Config"]["Env"])
    if env.get("HERMES_DASHBOARD") != "1":
        pytest.skip("the hermes-dashboard plugin is not enabled in this stack")
    r = _docker_exec(gateway, "/command/s6-svstat", "-u", "/run/service/dashboard")
    assert r.stdout.strip() == "true", f"the s6 dashboard service is not up: {r.stdout or r.stderr!r}"
    r = _docker_exec(gateway, "curl", "-fsS", "http://localhost:9119/api/status")
    assert r.returncode == 0, f"the dashboard does not answer: {r.stderr!r}"
    assert json.loads(r.stdout)["gateway_running"] is True, "the dashboard reports the gateway stopped"


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
