"""Plan C acceptance — verify Hermes' Docker socket and root-group access are gone.

These tests require the stack to be running with the Plan C compose
changes applied. They do live `docker exec` / `docker inspect` against
the hermes-gateway and hermes-dashboard containers, so they assume:
  - `make up` (or equivalent) brought the stack up.
  - hermes-gateway and hermes-dashboard are both running and healthy.

If neither container is up, the suite skips rather than fails — Plan C
is a runtime invariant, not a static one.
"""
from __future__ import annotations

import json
import subprocess

import pytest

GATEWAY = "ordo-ai-stack-hermes-gateway-1"
DASHBOARD = "ordo-ai-stack-hermes-dashboard-1"


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


def test_hermes_gateway_has_no_docker_sock(gateway: str):
    """/var/run/docker.sock must not exist inside hermes-gateway. The mount
    was the prompt-injection escape hatch; Plan C removes it."""
    r = _docker_exec(gateway, "test", "-S", "/var/run/docker.sock")
    assert r.returncode != 0, (
        "FAIL: /var/run/docker.sock present in hermes-gateway — Plan C regression"
    )


def test_hermes_dashboard_has_no_docker_sock(dashboard: str):
    r = _docker_exec(dashboard, "test", "-S", "/var/run/docker.sock")
    assert r.returncode != 0, (
        "FAIL: /var/run/docker.sock present in hermes-dashboard"
    )


def test_hermes_gateway_not_in_root_group(gateway: str):
    """`group_add: ['0']` must NOT be set; that was paired with the docker.sock
    mount to grant non-root hermes access to root:root mode-660 socket on
    Docker Desktop. With the socket gone, the root-group elevation is gone too."""
    parsed = _inspect(gateway)
    group_add = parsed["HostConfig"].get("GroupAdd", []) or []
    assert "0" not in group_add, (
        f"FAIL: group_add ['0'] still present (root-group access): {group_add!r}"
    )


def test_hermes_dashboard_not_in_root_group(dashboard: str):
    parsed = _inspect(dashboard)
    group_add = parsed["HostConfig"].get("GroupAdd", []) or []
    assert "0" not in group_add


def test_hermes_can_reach_ops_controller(gateway: str):
    """The whole point of Task 8: Hermes must still be able to call
    ops-controller for privileged verbs. The Hermes image is python-based
    and ships neither wget nor curl, so probe via stdlib urllib."""
    r = _docker_exec(
        gateway, "python3", "-c",
        "import urllib.request, sys;"
        "sys.exit(0 if urllib.request.urlopen('http://ops-controller:9000/health', timeout=5).status == 200 else 1)",
    )
    assert r.returncode == 0, (
        f"FAIL: hermes-gateway cannot reach ops-controller — stdout={r.stdout!r} stderr={r.stderr!r}"
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
