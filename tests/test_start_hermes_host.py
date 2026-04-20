"""Static lint checks for scripts/start-hermes-host.sh.

These tests verify structural properties without running the script (no hermes
install required in CI). Manual smoke validation is documented in
docs/hermes-agent.md.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "start-hermes-host.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_script_exists():
    assert SCRIPT.is_file(), f"{SCRIPT} missing"


def test_script_has_bash_shebang():
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", f"unexpected shebang: {first_line!r}"


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash -n requires POSIX bash (Git Bash/WSL path handling unreliable on Windows)",
)
def test_script_parses_as_bash():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_env_var_defaults_match_stack_conventions():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "${MODEL_GATEWAY_PORT:-11435}" in script
    assert "${MCP_GATEWAY_PORT:-8811}" in script
    assert "${LITELLM_MASTER_KEY:-local}" in script


def test_script_references_expected_paths():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "vendor/hermes-agent" in script
    assert "data/hermes" in script
    assert "docker compose up -d" in script


def test_script_stops_openclaw_defensively():
    """Operator runs Hermes OR OpenClaw — never both. Script stops any in-flight OpenClaw."""
    script = SCRIPT.read_text(encoding="utf-8")
    assert "openclaw-gateway" in script, "script should stop openclaw-gateway if running"


def test_env_example_has_hermes_section():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "Hermes Agent" in text
    assert "HERMES_HOME" in text
