"""The `codex` wrapper Hermes runs Codex through (services/hermes/codex).

It must keep the ChatGPT sign-in in its folder on the brain volume, never run a task without one
(exit 78 with the operator's fix), still allow the sign-in itself, drop any inherited API key or base
URL so a run always uses the subscription, and keep the CLI pinned.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "services" / "hermes" / "codex"
REAL_CLI = "/opt/codex/node_modules/.bin/codex"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def wrapper_with_stub_cli(tmp_path: Path) -> Path:
    """A copy of the wrapper whose exec target is a stub that prints what the CLI would receive."""
    stub = tmp_path / "codex-stub"
    stub.write_text('#!/bin/sh\necho "args=$*"\nenv | sort\n', newline="\n")
    stub.chmod(0o755)
    source = WRAPPER.read_text(encoding="utf-8")
    assert source.count(REAL_CLI) == 1
    copy = tmp_path / "codex"
    copy.write_text(source.replace(REAL_CLI, str(stub)), newline="\n")
    return copy


def run(wrapper: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(wrapper), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **env})


def test_runs_the_cli_signed_in_and_only_on_the_subscription(tmp_path):
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "auth.json").write_text('{"tokens": {}}')
    (tmp_path / "bin").mkdir()
    result = run(wrapper_with_stub_cli(tmp_path / "bin"), "exec", "hello", env={"HOME": str(tmp_path), "OPENAI_API_KEY": "sk-should-not-pass",
                                       "OPENAI_BASE_URL": "http://elsewhere"})
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert "args=-c check_for_update_on_startup=false exec hello" in out
    assert f"CODEX_HOME={tmp_path}/codex" in out
    assert not any(line.startswith(("OPENAI_API_KEY=", "OPENAI_BASE_URL=")) for line in out)


def test_refuses_a_task_without_a_sign_in_and_says_how(tmp_path):
    (tmp_path / "bin").mkdir()
    result = run(wrapper_with_stub_cli(tmp_path / "bin"), "exec", "hello", env={"HOME": str(tmp_path)})
    assert result.returncode == 78
    assert result.stdout == ""  # the CLI never ran
    assert "codex login --device-auth" in result.stderr


def test_sign_in_and_version_work_without_a_sign_in(tmp_path):
    (tmp_path / "bin").mkdir()
    wrapper = wrapper_with_stub_cli(tmp_path / "bin")
    for args in (["login", "--device-auth"], ["--version"]):
        result = run(wrapper, *args, env={"HOME": str(tmp_path)})
        assert result.returncode == 0, result.stderr


def test_the_image_pins_the_cli_and_installs_the_wrapper():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG CODEX_VERSION=\d+\.\d+\.\d+$", dockerfile, re.M), "pin an exact version"
    assert '"@openai/codex@${CODEX_VERSION}"' in dockerfile
    assert "COPY codex /usr/local/bin/codex" in dockerfile.splitlines()


def test_managed_config_pins_the_subscription_and_disables_shell_snapshots():
    """Codex's managed config outranks user config and CLI overrides, so even a run of the binary
    that skips the wrapper stays on the ChatGPT subscription and never writes shell snapshots."""
    import tomllib
    managed = tomllib.loads((ROOT / "services" / "hermes" / "codex-managed-config.toml").read_text(encoding="utf-8"))
    assert managed["forced_login_method"] == "chatgpt"
    assert managed["model_provider"] == "openai"
    assert managed["features"]["shell_snapshot"] is False
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY codex-managed-config.toml /etc/codex/managed_config.toml" in dockerfile.splitlines()
