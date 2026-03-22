"""Tests for scripts/validate_openclaw_config.py."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "validate_openclaw_config.py"


def _run(path: str | None, *extra: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT)]
    if path:
        cmd.append(path)
    cmd.extend(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


def test_valid_example_passes(tmp_path):
    """Valid minimal gateway config passes."""
    p = tmp_path / "openclaw.json"
    p.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "gateway": {
                            "baseUrl": "http://model-gateway:11435/v1",
                            "apiKey": "ollama-local",
                            "api": "openai-responses",
                            "headers": {"X-Service-Name": "openclaw"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    r = _run(str(p))
    assert r.returncode == 0, r.stderr


def test_missing_gateway_fails(tmp_path):
    """Missing gateway provider fails."""
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"models": {"providers": {}}}), encoding="utf-8")
    r = _run(str(p))
    assert r.returncode == 1
