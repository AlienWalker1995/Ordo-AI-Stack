"""Unit tests for ops-controller stats helpers and /stats/services endpoint."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("docker", MagicMock())

_path = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_controller_main", _path)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


def test_cpu_pct_from_stats_basic():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 750_000_000}, "system_cpu_usage": 9_000_000_000},
    }
    # cpu_delta=250M, system_delta=1B, cpus=4 → (.25)*4*100 = 100.0
    assert oc._cpu_pct_from_stats(stats) == 100.0


def test_cpu_pct_from_stats_zero_system_delta():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 100, "online_cpus": 2},
        "precpu_stats": {"cpu_usage": {"total_usage": 1}, "system_cpu_usage": 100},
    }
    assert oc._cpu_pct_from_stats(stats) == 0.0


def test_cpu_pct_from_stats_missing_fields():
    assert oc._cpu_pct_from_stats({}) == 0.0
    assert oc._cpu_pct_from_stats({"cpu_stats": {}}) == 0.0


def test_mem_from_stats_cgroup_v2():
    # docker stats subtracts inactive_file (v2) from usage
    stats = {"memory_stats": {
        "usage": 2_000_000_000,
        "stats": {"inactive_file": 500_000_000},
        "limit": 10_000_000_000,
    }}
    gb, pct = oc._mem_from_stats(stats)
    assert gb == 1.5
    assert pct == 15.0


def test_mem_from_stats_cgroup_v1_fallback_to_cache():
    stats = {"memory_stats": {
        "usage": 1_500_000_000,
        "stats": {"cache": 500_000_000},
        "limit": 4_000_000_000,
    }}
    gb, pct = oc._mem_from_stats(stats)
    assert gb == 1.0
    assert pct == 25.0


def test_mem_from_stats_empty():
    assert oc._mem_from_stats({}) == (0.0, 0.0)
    assert oc._mem_from_stats({"memory_stats": {}}) == (0.0, 0.0)


def test_container_host_pids_parses_docker_top():
    c = MagicMock()
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [["1234", "python3"], ["5678", "llama-server"]],
    }
    assert oc._container_host_pids(c) == [1234, 5678]


def test_container_host_pids_handles_empty_or_missing():
    c = MagicMock()
    c.top.return_value = {"Titles": ["PID", "COMMAND"], "Processes": None}
    assert oc._container_host_pids(c) == []
    c2 = MagicMock()
    c2.top.return_value = {}
    assert oc._container_host_pids(c2) == []


def test_container_host_pids_swallows_exceptions():
    c = MagicMock()
    c.top.side_effect = RuntimeError("container not running")
    assert oc._container_host_pids(c) == []


def test_container_host_pids_skips_non_numeric_rows():
    c = MagicMock()
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [["1234", "python3"], ["bad", "x"], [], ["9999", "comfyui"]],
    }
    assert oc._container_host_pids(c) == [1234, 9999]


class _MI:
    total = int(24e9)
    used = int(8e9)


class _UT:
    gpu = 42


class _P:
    def __init__(self, pid, mem):
        self.pid = pid
        self.usedGpuMemory = mem


def _patch_nvml(monkeypatch, compute_procs, graphics_procs=None):
    import pynvml
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlShutdown", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", lambda i: object())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo", lambda h: _MI())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetUtilizationRates", lambda h: _UT())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", lambda h: compute_procs)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", lambda h: graphics_procs or [])


def test_nvml_vraam_by_pid_happy(monkeypatch):
    _patch_nvml(monkeypatch, [_P(1234, int(6e9)), _P(5678, int(1e9))])
    pid_map, gpu = oc._nvml_vraam_by_pid()
    assert pid_map == {1234: int(6e9), 5678: int(1e9)}
    assert gpu["total_gb"] == 24.0
    assert gpu["used_gb"] == 8.0
    assert gpu["utilization_pct"] == 42
    assert gpu["per_pid_available"] is True


def test_nvml_vraam_by_pid_windows_fallback(monkeypatch):
    # On WSL2/WDDM, usedGpuMemory is None — flag goes to False
    _patch_nvml(monkeypatch, [_P(1234, None)])
    pid_map, gpu = oc._nvml_vraam_by_pid()
    assert pid_map == {}
    assert gpu["per_pid_available"] is False
    assert gpu["total_gb"] == 24.0  # aggregate still works


def test_nvml_vraam_by_pid_init_fails(monkeypatch):
    import pynvml
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: (_ for _ in ()).throw(Exception("no gpu")))
    pid_map, gpu = oc._nvml_vraam_by_pid()
    assert pid_map == {}
    assert gpu == {"total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}

from fastapi.testclient import TestClient  # noqa: E402 — needs the env-var setup above

VALID_TOKEN = "test-token"


def _mk_container(name, service, status="running", stats_sample=None, pids=None):
    c = MagicMock()
    c.name = name
    c.status = status
    c.labels = {"com.docker.compose.service": service, "com.docker.compose.project": "ordo-ai-stack"}
    c.stats.return_value = stats_sample or {
        "cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 10_000_000_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 750_000_000}, "system_cpu_usage": 9_000_000_000},
        "memory_stats": {"usage": 1_500_000_000, "stats": {"inactive_file": 500_000_000}, "limit": 10_000_000_000},
    }
    c.top.return_value = {
        "Titles": ["PID", "COMMAND"],
        "Processes": [[str(p), "x"] for p in (pids or [])],
    }
    return c


@pytest.fixture()
def stats_client(monkeypatch):
    monkeypatch.setattr(oc, "OPS_CONTROLLER_TOKEN", VALID_TOKEN)
    return TestClient(oc.app, raise_server_exceptions=False)


def test_stats_services_requires_auth(stats_client):
    r = stats_client.get("/stats/services")
    assert r.status_code == 401


def test_stats_services_basic_merge(stats_client, monkeypatch):
    containers = [
        _mk_container("ordo-comfyui-1", "comfyui", pids=[1234]),
        _mk_container("ordo-webui-1", "open-webui", pids=[2222]),
    ]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [_P(1234, int(6e9))])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    d = r.json()
    assert d["vram_aggregate_unavailable"] is False
    assert d["gpu"]["total_gb"] == 24.0
    assert d["services"]["comfyui"]["running"] is True
    assert d["services"]["comfyui"]["vram_gb"] == 6.0
    assert d["services"]["comfyui"]["vram_pct"] == 25.0
    assert d["services"]["open-webui"]["vram_gb"] == 0.0
    assert d["services"]["open-webui"]["cpu_pct"] == 100.0


def test_stats_services_stopped_container(stats_client, monkeypatch):
    containers = [_mk_container("ordo-n8n-1", "n8n", status="exited")]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    assert d["services"]["n8n"]["running"] is False
    assert d["services"]["n8n"]["cpu_pct"] == 0.0


def test_stats_services_vram_unavailable(stats_client, monkeypatch):
    containers = [_mk_container("ordo-comfyui-1", "comfyui", pids=[1234])]
    monkeypatch.setattr(oc, "_get_containers", lambda: containers)
    _patch_nvml(monkeypatch, [_P(1234, None)])  # WSL2 fallback
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    assert d["vram_aggregate_unavailable"] is True
    assert d["services"]["comfyui"]["vram_gb"] == 0.0


def test_stats_services_docker_list_fails(stats_client, monkeypatch):
    def boom():
        raise RuntimeError("docker daemon down")
    monkeypatch.setattr(oc, "_get_containers", boom)
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert r.status_code == 200
    d = r.json()
    assert d["services"] == {}
    assert d["vram_aggregate_unavailable"] is True


def test_stats_services_stats_fetch_error_per_container(stats_client, monkeypatch):
    c = _mk_container("ordo-comfyui-1", "comfyui", pids=[1234])
    c.stats.side_effect = RuntimeError("stats failed")
    monkeypatch.setattr(oc, "_get_containers", lambda: [c])
    _patch_nvml(monkeypatch, [])
    r = stats_client.get("/stats/services", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    d = r.json()
    # Container is running but stats failed — row still present, zeros
    assert d["services"]["comfyui"]["running"] is True
    assert d["services"]["comfyui"]["cpu_pct"] == 0.0