"""Locks ops-api's dynamic MCP service allowlist.

`mcp-gateway` was retired from ALLOWED_SERVICES: on this branch MCP servers are
rendered as compose services named `mcp-<server_id>` carrying the label
`ordo.mcp=true`. `_service_allowed()` accepts the static allowlist plus any
`mcp-*` service that the rendered `COMPOSE_PROJECT_DIR/docker-compose.yml`
actually labels `ordo.mcp: "true"`, so a new MCP plugin needs no edit here.

The host python has no `docker` package installed and `services/ops-api/main.py`
imports `docker` at module top, so a minimal stub is installed into
`sys.modules` before the module is loaded via `importlib`. `tests/conftest.py`
already points `AUDIT_LOG_PATH` at a writable temp path for exactly this import.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

if "docker" not in sys.modules:
    docker_stub = types.ModuleType("docker")
    docker_stub.from_env = lambda: types.SimpleNamespace()
    sys.modules["docker"] = docker_stub

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_API_MAIN = REPO_ROOT / "services" / "ops-api" / "main.py"

_spec = importlib.util.spec_from_file_location("ops_api_main", OPS_API_MAIN)
ops_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops_main)


def test_mcp_services_are_allowed_when_labelled_in_the_rendered_compose(tmp_path, monkeypatch):
    import yaml

    (tmp_path / "docker-compose.yml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "mcp-searxng": {"image": "x", "labels": {"ordo.mcp": "true"}},
                    "mcp-evil": {"image": "x"},
                }
            }
        )
    )
    monkeypatch.setattr(ops_main, "COMPOSE_PROJECT_DIR", str(tmp_path))
    assert ops_main._service_allowed("llamacpp")
    assert ops_main._service_allowed("mcp-searxng")
    assert not ops_main._service_allowed("mcp-evil")
    assert not ops_main._service_allowed("mcp-gateway")
