"""Verify Hermes' bind-mounts cannot see decrypted runtime secrets and
that high-value tokens don't appear as plaintext env vars in containers."""
import json
import os
import subprocess

import pytest

# Container names are "<compose project>-<service>-1"; the service that runs
# the Hermes agent is `agent` (renamed from hermes-gateway). Derive the
# project prefix from COMPOSE_PROJECT_NAME so a project rename doesn't
# silently re-break this suite into a permanent skip.
COMPOSE_PROJECT_NAME = os.environ.get("COMPOSE_PROJECT_NAME", "ordo")


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
    name = f"{COMPOSE_PROJECT_NAME}-agent-1"
    if not _container_running(name):
        pytest.skip(f"{name} not running; bring the stack up to run this suite")
    return name


def test_runtime_env_not_visible_in_workspace(hermes_gateway: str):
    """From inside hermes-gateway, /workspace/.env must NOT exist: /workspace/data is the
    data root, and no secret file is materialized there (secrets live in the SOPS store and
    out/, see docs/runbooks/secrets.md)."""
    r = _docker_exec(hermes_gateway, "test", "-f", "/workspace/.env")
    assert r.returncode != 0, (
        "FAIL: /workspace/.env exists inside Hermes — secret leakage path open"
    )


def test_runtime_secrets_dir_not_visible_in_workspace(hermes_gateway: str):
    """No path under /workspace should hold the decrypted runtime secrets."""
    r = _docker_exec(
        hermes_gateway,
        "find", "/workspace", "-maxdepth", "3", "-name", "discord_bot_token",
    )
    assert r.stdout.strip() == "", (
        f"FAIL: discord_bot_token visible at {r.stdout!r}"
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
    r = _docker_exec(hermes_gateway, "test", "-r", "/run/secrets/discord_bot_token")
    assert r.returncode == 0, (
        "FAIL: /run/secrets/discord_bot_token not readable inside container"
    )


def _project_containers() -> list[str]:
    r = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT_NAME}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip("docker is not reachable")
    return [line for line in r.stdout.splitlines() if line.strip()]


def test_file_secrets_are_not_in_any_container_environment():
    """Every running container that mounts a file secret (its `ordo.secret-file.<file>` labels,
    ordo/secret_files.py) has no variable of that key's name, only the path variable. The one
    exception is a value that is itself a path (`TS_AUTHKEY=file:/run/secrets/...`). Failures name
    the container and the key, never a value."""
    offenders = []
    for name in _project_containers():
        inspect = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True)
        config = json.loads(inspect.stdout)[0]["Config"]
        keys = {label[len("ordo.secret-file."):].upper()
                for label in (config.get("Labels") or {}) if label.startswith("ordo.secret-file.")}
        for entry in config.get("Env") or []:
            key, _, value = entry.partition("=")
            if key in keys and not value.startswith("file:/run/secrets/"):
                offenders.append(f"{name}: {key}")
    assert offenders == [], f"FAIL: file secrets also present as env values: {offenders}"
