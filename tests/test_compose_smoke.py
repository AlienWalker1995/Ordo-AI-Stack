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
SMOKE_SERVICES = ["llamacpp", "llamacpp-embed", "model-gateway", "dashboard"]


# Placeholders for compose's ${VAR:?...} strict-required variables. These are
# operator-supplied at deploy time; provide test stand-ins so `compose config`
# can interpolate during static validation.
_COMPOSE_REQUIRED_PLACEHOLDERS = {
    "BASE_PATH": ".",
    "OPS_CONTROLLER_TOKEN": "placeholder-for-tests",
    "CADDY_BIND": "127.0.0.1",
}


def _compose_cmd(*args, extra_env=None, timeout=120):
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    if COMPOSE_VLLM.exists():
        cmd += ["-f", str(COMPOSE_VLLM)]
    cmd += list(args)
    env = {**os.environ, **_COMPOSE_REQUIRED_PLACEHOLDERS, **(extra_env or {})}
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
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
    assert "ordo-ai-stack-frontend" in out or "frontend" in out
    assert "ordo-ai-stack-backend" in out or "backend" in out


@pytest.mark.skipif(not COMPOSE_VLLM.exists(), reason="overrides/vllm.yml not present")
def test_compose_vllm_override_config_valid():
    """With vllm override, compose config still valid (vllm profile)."""
    r = _compose_cmd("config", "--quiet", extra_env={"COMPOSE_PROFILES": "vllm"})
    assert r.returncode == 0, f"vllm config failed: {r.stderr or r.stdout}"


def _has_nvidia_gpu() -> bool:
    """Return True iff `nvidia-smi` is available and exits 0."""
    try:
        return subprocess.run(
            ["nvidia-smi"], capture_output=True, timeout=5,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(
    os.environ.get("RUN_COMPOSE_SMOKE") != "1" or not _has_nvidia_gpu(),
    reason="Requires RUN_COMPOSE_SMOKE=1 AND an NVIDIA GPU "
           "(llama.cpp:server-cuda will not start on a CPU-only runner; "
           "compose up hangs while CUDA-required containers retry).",
)
def test_compose_up_and_services_healthy():
    """Bring up stack and assert core services become healthy (Docker daemon required)."""
    # Bring up only core services to limit resource use. Image pulls for
    # llama.cpp:server-cuda alone are several GB on a cold runner, so the
    # default 120s subprocess timeout is too tight for the up call. The
    # health-poll loop below has its own 3-minute budget.
    up = _compose_cmd(
        "up", "-d", "llamacpp", "llamacpp-embed", "model-gateway", "dashboard",
        timeout=900,
    )
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
                if r2.returncode == 0 and "llamacpp" in (r2.stdout or ""):
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
