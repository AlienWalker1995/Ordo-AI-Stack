"""Verify Hermes' bind-mounts cannot see decrypted runtime secrets and
that high-value tokens don't appear as plaintext env vars in containers."""
import json
import subprocess

import pytest


def _docker_exec(container: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *cmd],
        capture_output=True,
        text=True,
    )


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


@pytest.fixture(scope="module")
def hermes_gateway() -> str:
    name = "ordo-ai-stack-hermes-gateway-1"
    if not _container_running(name):
        pytest.skip(f"{name} not running; bring the stack up to run this suite")
    return name


def test_runtime_env_not_visible_in_workspace(hermes_gateway: str):
    """From inside hermes-gateway, /workspace/.env (the runtime decrypted .env)
    must NOT exist. The runtime file lives at ~/.ai-toolkit/runtime/.env on the
    host, outside any bind-mount Hermes can see."""
    r = _docker_exec(hermes_gateway, "test", "-f", "/workspace/.env")
    assert r.returncode != 0, (
        "FAIL: /workspace/.env exists inside Hermes — secret leakage path open"
    )


def test_runtime_secrets_dir_not_visible_in_workspace(hermes_gateway: str):
    """No path under /workspace should hold the decrypted runtime secrets."""
    r = _docker_exec(
        hermes_gateway,
        "find", "/workspace", "-maxdepth", "3", "-name", "discord_token",
    )
    assert r.stdout.strip() == "", (
        f"FAIL: discord_token visible at {r.stdout!r}"
    )


def test_high_value_token_not_in_docker_inspect(hermes_gateway: str):
    """`docker inspect hermes-gateway` should not contain the plaintext Discord token."""
    inspect = subprocess.run(
        ["docker", "inspect", hermes_gateway],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(inspect.stdout)
    env = parsed[0]["Config"]["Env"]
    plaintext = [e for e in env if e.startswith("DISCORD_BOT_TOKEN=")]
    assert plaintext == [], (
        f"FAIL: plaintext DISCORD_BOT_TOKEN env var present: {plaintext}"
    )
    pointer = [e for e in env if e.startswith("DISCORD_BOT_TOKEN_FILE=")]
    assert pointer, (
        "FAIL: DISCORD_BOT_TOKEN_FILE pointer missing — wiring incomplete"
    )


def test_secret_file_inside_container_is_readable(hermes_gateway: str):
    """The Docker secret file should be readable by the service inside its container."""
    r = _docker_exec(hermes_gateway, "test", "-r", "/run/secrets/discord_token")
    assert r.returncode == 0, (
        "FAIL: /run/secrets/discord_token not readable inside container"
    )
