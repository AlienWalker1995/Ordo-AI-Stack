"""Our own services read a file-delivered secret from `<NAME>_FILE` (the rendered delivery).

The render mounts each secret at /run/secrets/<key lowercased> and sets `<NAME>_FILE` to it
(ordo/render/secret_files.py); every Python reader goes through the one helper, secret_env.read_secret.
These tests point `<NAME>_FILE` at a temp file with the plain variable unset, as in the container.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = ("OPS_CONTROLLER_TOKEN", "MODEL_GATEWAY_API_KEY", "LITELLM_MASTER_KEY",
                "DASHBOARD_LOCAL_LOGIN_TOKEN", "THROUGHPUT_RECORD_TOKEN")


@pytest.fixture
def file_secrets(tmp_path, monkeypatch):
    """Deliver each given secret as a file, the way the rendered compose does."""
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)

    def deliver(**values: str) -> None:
        for name, value in values.items():
            path = tmp_path / name.lower()
            path.write_text(value, encoding="utf-8")
            monkeypatch.setenv(f"{name}_FILE", str(path))
    return deliver


def _load_fresh(name: str, path: Path):
    """Import a module file under a private name, so its import-time reads see this test's env."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_settings_read_the_token_files(file_secrets):
    pytest.importorskip("fastapi")
    sys.path.insert(0, str(ROOT / "services" / "dashboard"))
    file_secrets(OPS_CONTROLLER_TOKEN="ops-from-file", DASHBOARD_LOCAL_LOGIN_TOKEN="login-from-file")
    settings = _load_fresh("dashboard_settings_under_test",
                           ROOT / "services" / "dashboard" / "dashboard" / "settings.py")
    assert settings.OPS_CONTROLLER_TOKEN == "ops-from-file"
    assert settings.DASHBOARD_LOCAL_LOGIN_TOKEN == "login-from-file"


def test_dashboard_reads_the_gateway_key_from_its_file(file_secrets):
    pytest.importorskip("fastapi")
    sys.path.insert(0, str(ROOT / "services" / "dashboard"))
    file_secrets(MODEL_GATEWAY_API_KEY="sk-from-file")
    readiness = _load_fresh("dashboard_readiness_under_test",
                            ROOT / "services" / "dashboard" / "dashboard" / "orchestration_readiness.py")
    assert readiness.MODEL_GATEWAY_API_KEY == "sk-from-file"


def test_the_gpu_gate_reads_its_ops_token_file(file_secrets):
    pytest.importorskip("aiohttp")
    sys.path.insert(0, str(ROOT / "services" / "gpu-gate"))
    file_secrets(OPS_CONTROLLER_TOKEN="gate-token-from-file")
    gate = _load_fresh("gpu_gate_under_test", ROOT / "services" / "gpu-gate" / "gate.py")
    assert gate.Config().ops_token == "gate-token-from-file"


def test_the_orchestration_adapter_reads_its_ops_token_file(file_secrets):
    pytest.importorskip("mcp.server.fastmcp", reason="mcp is a service dependency, not a CI test dependency")
    sys.path.insert(0, str(ROOT / "services" / "orchestration"))
    file_secrets(OPS_CONTROLLER_TOKEN="orch-token-from-file")
    server = _load_fresh("orchestration_server_under_test", ROOT / "services" / "orchestration" / "server.py")
    assert server.TOKEN == "orch-token-from-file"


def test_the_comfyui_mcp_reads_its_ops_token_file(file_secrets):
    pytest.importorskip("mcp.server.fastmcp", reason="mcp is a service dependency, not a CI test dependency")
    pytest.importorskip("requests")
    sys.path.insert(0, str(ROOT / "services" / "comfyui-mcp"))
    file_secrets(OPS_CONTROLLER_TOKEN="comfy-token-from-file")
    management = _load_fresh("comfyui_mcp_management_under_test",
                             ROOT / "services" / "comfyui-mcp" / "tools" / "management.py")
    assert management.OPS_CONTROLLER_TOKEN == "comfy-token-from-file"
