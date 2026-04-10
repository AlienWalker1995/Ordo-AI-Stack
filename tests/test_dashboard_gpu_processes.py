"""Tests for GPU process label resolution and /api/hardware/gpu-processes endpoint."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import psutil
from fastapi.testclient import TestClient


def _make_proc(cmdline_list: list[str], name: str) -> MagicMock:
    p = MagicMock()
    p.cmdline.return_value = cmdline_list
    p.name.return_value = name
    return p


def test_pid_label_llm() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["/app/llama-server", "--host", "0.0.0.0"], "llama-server")):
        assert app._pid_to_service_label(1234) == "LLM"


def test_pid_label_llama_underscore() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["llama_server", "--model", "foo.gguf"], "llama_server")):
        assert app._pid_to_service_label(1235) == "LLM"


def test_pid_label_comfyui() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["python3.12", "/root/ComfyUI/main.py"], "python3.12")):
        assert app._pid_to_service_label(5678) == "ComfyUI"


def test_pid_label_embed() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["python3.12", "-m", "embed_server"], "python3.12")):
        assert app._pid_to_service_label(9012) == "Embed"


def test_pid_label_fallback_python() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["python3.12", "unknown_service.py"], "python3.12")):
        assert app._pid_to_service_label(3456) == "Python"


def test_pid_label_non_python_truncates() -> None:
    import dashboard.app as app
    with patch("psutil.Process", return_value=_make_proc(["some-long-process-name", "--flag"], "some-long-process-name")):
        result = app._pid_to_service_label(7890)
        assert len(result) <= 12


def test_pid_label_process_gone() -> None:
    import dashboard.app as app
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(pid=9999)):
        assert app._pid_to_service_label(9999) == "pid:9999"


def test_pid_label_access_denied() -> None:
    import dashboard.app as app
    with patch("psutil.Process", side_effect=psutil.AccessDenied(pid=8888)):
        assert app._pid_to_service_label(8888) == "pid:8888"


# ── Endpoint tests ────────────────────────────────────────

class _MockMemInfo:
    total = int(32e9)
    used  = int(20e9)

class _MockUtil:
    gpu = 75

class _MockProc:
    def __init__(self, pid: int, mem: int) -> None:
        self.pid = pid
        self.usedGpuMemory = mem


def _patch_nvml(monkeypatch, procs: list) -> None:
    import pynvml
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlShutdown", lambda: None)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", lambda i: object())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo", lambda h: _MockMemInfo())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetUtilizationRates", lambda h: _MockUtil())
    monkeypatch.setattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", lambda h: procs)


def test_gpu_processes_endpoint_returns_structure(monkeypatch) -> None:
    import dashboard.app as app
    _patch_nvml(monkeypatch, [_MockProc(1234, int(20e9)), _MockProc(5678, int(5e9))])
    monkeypatch.setattr(app, "_pid_to_service_label", lambda pid: "LLM" if pid == 1234 else "ComfyUI")
    client = TestClient(app.app)
    r = client.get("/api/hardware/gpu-processes")
    assert r.status_code == 200
    d = r.json()
    assert d["total_gb"] == 32.0
    assert d["used_gb"] == 20.0
    assert d["utilization_pct"] == 75
    assert len(d["processes"]) == 2
    # sorted descending by vram_gb
    assert d["processes"][0]["label"] == "LLM"
    assert d["processes"][0]["vram_gb"] == 20.0
    assert d["processes"][1]["label"] == "ComfyUI"
    assert d["processes"][1]["vram_gb"] == 5.0


def test_gpu_processes_vram_pct_sums_to_at_most_100(monkeypatch) -> None:
    import dashboard.app as app
    _patch_nvml(monkeypatch, [_MockProc(1, int(20e9)), _MockProc(2, int(5e9))])
    monkeypatch.setattr(app, "_pid_to_service_label", lambda pid: "LLM" if pid == 1 else "ComfyUI")
    client = TestClient(app.app)
    d = client.get("/api/hardware/gpu-processes").json()
    total_pct = sum(p["vram_pct"] for p in d["processes"])
    assert total_pct <= 100.0


def test_gpu_processes_endpoint_handles_nvml_unavailable(monkeypatch) -> None:
    import pynvml

    import dashboard.app as app
    monkeypatch.setattr(pynvml, "nvmlInit", lambda: (_ for _ in ()).throw(Exception("no GPU")))
    client = TestClient(app.app)
    r = client.get("/api/hardware/gpu-processes")
    assert r.status_code == 200
    d = r.json()
    assert d["processes"] == []
    assert d["utilization_pct"] == 0
    assert d["total_gb"] == 0.0


def test_gpu_processes_no_auth_required(monkeypatch) -> None:
    """Endpoint must be accessible without Authorization header."""
    import dashboard.app as app
    _patch_nvml(monkeypatch, [])
    monkeypatch.setattr(app, "_pid_to_service_label", lambda pid: "other")
    client = TestClient(app.app)
    r = client.get("/api/hardware/gpu-processes")
    assert r.status_code == 200
