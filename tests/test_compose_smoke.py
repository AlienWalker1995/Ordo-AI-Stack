"""Compose config and optional runtime smoke tests.

- Config tests: validate docker-compose.yml (and optional vllm override) parse and merge.
- Runtime smoke: set RUN_COMPOSE_SMOKE=1 to run 'compose up -d' and assert key services healthy
  (requires Docker daemon; use in CI or locally).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
COMPOSE_VLLM = REPO_ROOT / "overrides" / "vllm.yml"

# Services that must be healthy for "smoke" (long-running core stack)
SMOKE_SERVICES = ["ollama", "model-gateway", "dashboard"]


def _compose_cmd(*args, extra_env=None):
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if COMPOSE_VLLM.exists():
        cmd += ["-f", str(COMPOSE_VLLM)]
    cmd += list(args)
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_compose_config_valid():
    """docker compose config succeeds for main compose file."""
    r = _compose_cmd("config", "--quiet")
    assert r.returncode == 0, f"config failed: {r.stderr or r.stdout}"


def test_compose_config_includes_networks():
    """Compose config defines frontend and backend networks."""
    r = _compose_cmd("config")
    assert r.returncode == 0
    out = r.stdout
    assert "ai-toolkit-frontend" in out or "frontend" in out
    assert "ai-toolkit-backend" in out or "backend" in out


@pytest.mark.skipif(not COMPOSE_VLLM.exists(), reason="overrides/vllm.yml not present")
def test_compose_vllm_override_config_valid():
    """With vllm override, compose config still valid (vllm profile)."""
    r = _compose_cmd("config", "--quiet", extra_env={"COMPOSE_PROFILES": "vllm"})
    assert r.returncode == 0, f"vllm config failed: {r.stderr or r.stdout}"


@pytest.mark.skipif(os.environ.get("RUN_COMPOSE_SMOKE") != "1", reason="Set RUN_COMPOSE_SMOKE=1 to run")
def test_compose_up_and_services_healthy():
    """Bring up stack and assert core services become healthy (Docker daemon required)."""
    # Bring up only core services to limit resource use
    up = _compose_cmd("up", "-d", "ollama", "model-gateway", "dashboard")
    assert up.returncode == 0, f"compose up failed: {up.stderr or up.stdout}"

    try:
        # Wait for health (poll up to 3 minutes)
        for _ in range(36):
            r = _compose_cmd("ps", "--format", "json")
            if r.returncode != 0:
                time.sleep(5)
                continue
            # Expect running and healthy for the services we started
            out = r.stdout
            if "healthy" in out or "running" in out:
                # Quick sanity: at least one service running
                r2 = _compose_cmd("ps", "--status", "running")
                if r2.returncode == 0 and "ollama" in (r2.stdout or ""):
                    break
            time.sleep(5)
        else:
            pytest.fail("Services did not become healthy within 3 minutes")

        # Optional: curl health endpoints
        for name, path in [("model-gateway", "http://localhost:11435/health")]:
            r3 = subprocess.run(
                ["curl", "-sf", path],
                capture_output=True,
                timeout=5,
            )
            assert r3.returncode == 0, f"{name} health check failed"
    finally:
        _compose_cmd("down", "--remove-orphans")
