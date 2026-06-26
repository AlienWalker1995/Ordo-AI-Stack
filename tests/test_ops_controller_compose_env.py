"""Tests for ops-controller compose secret injection.

Covers `_load_runtime_env` (parsing the SOPS-decrypted runtime env mounted at
RUNTIME_ENV_FILE) and `_compose_env` (merging it into the docker-compose
subprocess environment), which is what lets ops-controller recreate
secret-dependent services with real values instead of leaving them unset.

No real secrets here — all values are fabricated fixtures.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock docker before loading ops-controller (avoids requiring the docker package).
sys.modules["docker"] = MagicMock()

# Load ops-controller/main.py (folder has a hyphen, not a valid module name).
_ops_controller_path = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_controller_main", _ops_controller_path)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


def _write_runtime(tmp: str, body: str) -> Path:
    p = Path(tmp) / "runtime.env"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_runtime_env_parses_and_strips_quotes():
    with tempfile.TemporaryDirectory() as tmp:
        oc.RUNTIME_ENV_FILE = _write_runtime(tmp, (
            "# a comment line\n"
            "\n"
            "OAUTH2_PROXY_COOKIE_SECRET=abcdef0123456789abcdef0123456789\n"
            'N8N_OWNER_PASSWORD="quoted value"\n'
            "SEARXNG_SECRET='single'\n"
            "MALFORMED_LINE_NO_EQUALS\n"
        ))
        env = oc._load_runtime_env()
    assert env["OAUTH2_PROXY_COOKIE_SECRET"] == "abcdef0123456789abcdef0123456789"
    assert env["N8N_OWNER_PASSWORD"] == "quoted value"
    assert env["SEARXNG_SECRET"] == "single"
    assert "MALFORMED_LINE_NO_EQUALS" not in env
    assert "" not in env  # blank line produced no key


def test_load_runtime_env_missing_file_returns_empty():
    oc.RUNTIME_ENV_FILE = Path(tempfile.gettempdir()) / "ordo-absent-runtime-env.xyz"
    if oc.RUNTIME_ENV_FILE.exists():
        oc.RUNTIME_ENV_FILE.unlink()
    assert oc._load_runtime_env() == {}


def test_load_runtime_env_directory_degrades_gracefully():
    # If the host file was missing at compose time, Docker can auto-create the
    # mount source as a directory; reading it must not raise.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "runtime.env"
        d.mkdir()
        oc.RUNTIME_ENV_FILE = d
        assert oc._load_runtime_env() == {}


def test_compose_env_runtime_overrides_process_env():
    """A placeholder in the process env is overridden by the real runtime value."""
    with tempfile.TemporaryDirectory() as tmp:
        oc.RUNTIME_ENV_FILE = _write_runtime(tmp, "OAUTH2_PROXY_COOKIE_SECRET=realsecret\n")
        oc.BASE_PATH = "/workspace-test"
        with patch.dict(os.environ, {
            "OAUTH2_PROXY_COOKIE_SECRET": "placeholder",
            "OPERATOR_HOME": "/c/Users/op",
        }, clear=False):
            env = oc._compose_env()
    assert env["OAUTH2_PROXY_COOKIE_SECRET"] == "realsecret"  # runtime wins over process env
    assert env["BASE_PATH"] == "/workspace-test"
    assert env["HOME"] == "/c/Users/op"  # OPERATOR_HOME pins HOME for ${HOME} secret mounts


def test_compose_env_extra_overrides_everything():
    with tempfile.TemporaryDirectory() as tmp:
        oc.RUNTIME_ENV_FILE = _write_runtime(tmp, "DATA_PATH=/runtime/data\n")
        env = oc._compose_env({"DATA_PATH": "/explicit"})
    assert env["DATA_PATH"] == "/explicit"
