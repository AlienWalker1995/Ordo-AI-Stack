"""The `claude` wrapper Hermes runs Claude Code through (services/hermes/claude-code).

It must hand the subscription token from its file secret to the CLI only, never run without one
(exit 78 with the operator's fix), drop any inherited API key or base URL so a run always uses the
subscription, and keep the CLI pinned. The token must never be bridged into the agent's environment.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "services" / "hermes" / "claude-code"
REAL_CLI = "/opt/claude-code/node_modules/.bin/claude"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def wrapper_with_stub_cli(tmp_path: Path) -> Path:
    """A copy of the wrapper whose exec target is a stub that prints what the CLI would receive."""
    stub = tmp_path / "claude-stub"
    stub.write_text('#!/bin/sh\necho "args=$*"\nenv | sort\n', newline="\n")
    stub.chmod(0o755)
    source = WRAPPER.read_text(encoding="utf-8")
    assert source.count(REAL_CLI) == 1
    copy = tmp_path / "claude"
    copy.write_text(source.replace(REAL_CLI, str(stub)), newline="\n")
    return copy


def run(wrapper: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(wrapper), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **env})


def test_runs_the_cli_with_the_token_from_the_file_and_only_the_subscription(tmp_path):
    token = tmp_path / "claude_code_oauth_token"
    token.write_text("sk-ant-oat01-example")
    result = run(wrapper_with_stub_cli(tmp_path), "-p", "hello", env={
        "CLAUDE_CODE_OAUTH_TOKEN_FILE": str(token), "HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "sk-ant-api-should-not-pass", "ANTHROPIC_BASE_URL": "http://elsewhere"})
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert "args=-p hello" in out
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-example" in out
    assert "DISABLE_AUTOUPDATER=1" in out
    assert f"CLAUDE_CONFIG_DIR={tmp_path}/claude-code" in out
    assert not any(line.startswith(("ANTHROPIC_API_KEY=", "ANTHROPIC_BASE_URL=", "CLAUDE_CODE_OAUTH_TOKEN_FILE="))
                   for line in out)


def test_refuses_without_a_token_and_says_how_to_set_one(tmp_path):
    empty = tmp_path / "claude_code_oauth_token"
    empty.write_text("")
    for token_file in (empty, tmp_path / "absent"):
        result = run(wrapper_with_stub_cli(tmp_path), "-p", "hello",
                     env={"CLAUDE_CODE_OAUTH_TOKEN_FILE": str(token_file), "HOME": str(tmp_path)})
        assert result.returncode == 78
        assert result.stdout == ""  # the CLI never ran
        assert "ordo secrets set CLAUDE_CODE_OAUTH_TOKEN" in result.stderr


def test_version_works_without_a_token(tmp_path):
    result = run(wrapper_with_stub_cli(tmp_path), "--version",
                 env={"CLAUDE_CODE_OAUTH_TOKEN_FILE": str(tmp_path / "absent"), "HOME": str(tmp_path)})
    assert result.returncode == 0
    assert not any(line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") for line in result.stdout.splitlines())


def test_the_image_pins_the_cli_and_installs_the_wrapper():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+$", dockerfile, re.M), "pin an exact version"
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in dockerfile
    assert "COPY claude-code /usr/local/bin/claude" in dockerfile.splitlines()


def test_the_token_is_never_bridged_into_the_agent_environment():
    hook = (ROOT / "services" / "hermes" / "ordo-init.sh").read_text(encoding="utf-8")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in hook
