"""Static checks for the dockerized Hermes integration.

No Docker daemon required — pure file-content checks.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "hermes" / "Dockerfile"
ENTRYPOINT = REPO_ROOT / "hermes" / "entrypoint.sh"
DOCKERIGNORE = REPO_ROOT / "hermes" / ".dockerignore"
CATALOG = REPO_ROOT / "dashboard" / "services_catalog.py"


def _compose_services() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc.get("services", {})


def test_hermes_services_exist():
    services = _compose_services()
    assert "hermes-gateway" in services
    assert "hermes-dashboard" in services


def test_hermes_services_use_same_image():
    services = _compose_services()
    img_a = services["hermes-gateway"].get("image")
    img_b = services["hermes-dashboard"].get("image")
    assert img_a == img_b, f"expected shared image, got {img_a!r} vs {img_b!r}"


def test_hermes_services_build_from_hermes_dir():
    services = _compose_services()
    for svc in ("hermes-gateway", "hermes-dashboard"):
        build = services[svc].get("build")
        assert build, f"{svc} has no build section"
        assert build.get("context") == "./hermes", f"{svc} context != ./hermes"


@pytest.mark.skip(
    reason="Direct host port exposure on hermes-dashboard was removed when "
    "the stack moved to Caddy + oauth2-proxy SSO front door. The dashboard "
    "is now reachable only via the proxy on the host's tailnet IP; there is "
    "no direct ${HERMES_DASHBOARD_PORT:-9119} mapping to assert on. Test "
    "left in place as a marker — delete or rewrite if/when the architecture "
    "changes again."
)
def test_dashboard_port_is_env_overridable():
    svc = _compose_services()["hermes-dashboard"]
    ports = svc.get("ports", [])
    assert any("${HERMES_DASHBOARD_PORT:-9119}:9119" in p for p in ports), (
        f"expected env-overridable port mapping, got: {ports}"
    )


def test_hermes_services_mount_workspace_and_state():
    services = _compose_services()
    for svc in ("hermes-gateway", "hermes-dashboard"):
        vols = services[svc].get("volumes", [])
        assert any(":/workspace" in v for v in vols), f"{svc} missing /workspace mount"
        assert any(":/home/hermes/.hermes" in v for v in vols), (
            f"{svc} missing /home/hermes/.hermes mount"
        )


def test_hermes_services_depend_on_stack():
    services = _compose_services()
    required = ("model-gateway", "mcp-gateway", "dashboard")
    for svc in ("hermes-gateway", "hermes-dashboard"):
        deps = services[svc].get("depends_on") or {}
        for dep in required:
            assert dep in deps, f"{svc} missing depends_on: {dep}"
            assert deps[dep].get("condition") == "service_healthy", (
                f"{svc} depends_on {dep} must require service_healthy"
            )


def test_gateway_command_is_hermes_gateway():
    svc = _compose_services()["hermes-gateway"]
    cmd = svc.get("command")
    joined = " ".join(cmd) if isinstance(cmd, list) else (cmd or "")
    assert "hermes" in joined and "gateway" in joined, f"got: {cmd!r}"


def test_dashboard_command_binds_all_interfaces():
    svc = _compose_services()["hermes-dashboard"]
    cmd = svc.get("command") or []
    joined = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "dashboard" in joined, f"dashboard subcommand missing: {cmd}"
    assert "--host" in joined and "0.0.0.0" in joined, f"must bind 0.0.0.0: {cmd}"
    assert "--no-open" in joined, f"must use --no-open: {cmd}"


def test_gateway_healthcheck_uses_state_file():
    """Docker-mode doesn't create gateway.pid; use gateway_state.json instead."""
    svc = _compose_services()["hermes-gateway"]
    test_cmd = (svc.get("healthcheck") or {}).get("test") or []
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert "gateway_state.json" in joined, f"healthcheck must check gateway_state.json: {test_cmd}"


def test_dockerfile_exists_and_multistage():
    assert DOCKERFILE.is_file(), f"{DOCKERFILE} missing"
    src = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM node:" in src, "web-builder stage missing"
    assert "FROM python:3.11-slim" in src, "runtime stage missing"
    assert "ARG HERMES_PINNED_SHA" in src, "pinned SHA must be a build arg"


def test_entrypoint_is_bash_and_seeds_config():
    assert ENTRYPOINT.is_file(), f"{ENTRYPOINT} missing"
    src = ENTRYPOINT.read_text(encoding="utf-8")
    first_line = src.splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", f"unexpected shebang: {first_line!r}"
    assert "model.base_url" in src, "entrypoint must seed model.base_url"
    assert "model-gateway:11435" in src, "entrypoint must point at Docker DNS model-gateway:11435"
    assert "mcp_servers.gateway.url" in src, "entrypoint must seed mcp_servers.gateway.url"
    assert 'exec "$@"' in src, "entrypoint must exec the supplied command"


def test_dockerignore_exists():
    assert DOCKERIGNORE.is_file(), f"{DOCKERIGNORE} missing"


def test_services_catalog_hermes_uses_internal_dns():
    src = CATALOG.read_text(encoding="utf-8")
    assert "hermes-dashboard:9119" in src, (
        "catalog must probe internal DNS hermes-dashboard:9119"
    )
    assert "host.docker.internal:9119" not in src, (
        "catalog must not use host.docker.internal (host-mode residue)"
    )


def test_host_mode_files_removed():
    """Post-migration: host-mode bootstrap and tests must be gone."""
    start_host = REPO_ROOT / "scripts" / "start-hermes-host.sh"
    host_test = REPO_ROOT / "tests" / "test_start_hermes_host.py"
    assert not start_host.exists(), f"{start_host} should be deleted"
    assert not host_test.exists(), f"{host_test} should be deleted"
