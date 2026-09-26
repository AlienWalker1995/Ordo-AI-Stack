"""`/api/hardware`'s GPU fields come from ops-controller `GET /gpus`, the stack's one live GPU
reader (ordo/render/gpu_live.py). The dashboard converts its MiB readings to the decimal GB the UI
shows and never probes a GPU itself, so it needs no GPU reservation and works on AMD hosts."""
import asyncio

import dashboard.app as app

CARDS = [
    {"index": 0, "uuid": "GPU-bbb", "name": "NVIDIA GeForce RTX 5090", "vendor": "nvidia",
     "vram_total_mib": 32607, "vram_used_mib": 20426, "utilization_pct": 1, "temp_c": 41},
    {"index": 1, "uuid": "GPU-aaa", "name": "NVIDIA GeForce GTX 1070", "vendor": "nvidia",
     "vram_total_mib": 8192, "vram_used_mib": None, "utilization_pct": 11, "temp_c": None},
]


def _ops_answers(monkeypatch, code, body):
    calls = []

    async def fake_ops(method, path, request=None, **kwargs):
        calls.append((method, path))
        return code, body

    monkeypatch.setattr(app, "_ops_request", fake_ops)
    return calls


def test_hardware_stats_maps_the_ops_controller_gpus(monkeypatch):
    calls = _ops_answers(monkeypatch, 200, {"gpus": CARDS})
    stats = asyncio.run(app.hardware_stats())
    assert ("GET", "/gpus") in calls
    assert stats["gpus"] == [
        {"index": 0, "uuid": "GPU-bbb", "name": "NVIDIA GeForce RTX 5090",
         "vram_total_gb": 34.2, "vram_used_gb": 21.4, "vram_total_mib": 32607,
         "utilization_pct": 1, "temp_c": 41},
        {"index": 1, "uuid": "GPU-aaa", "name": "NVIDIA GeForce GTX 1070",
         "vram_total_gb": 8.6, "vram_used_gb": None, "vram_total_mib": 8192,
         "utilization_pct": 11, "temp_c": None},
    ]
    assert stats["gpu"] == {
        "name": "NVIDIA GeForce RTX 5090",
        "vram_used_gb": 21.4,
        "vram_total_gb": 34.2,
        "utilization_pct": 1,
        "memory_reading_reliable": True,
        "source": "ops-controller",
    }


def test_the_singular_gpu_flags_an_unreadable_used_value(monkeypatch):
    _ops_answers(monkeypatch, 200, {"gpus": [CARDS[1]]})
    gpu = asyncio.run(app.hardware_stats())["gpu"]
    assert gpu["vram_used_gb"] is None
    assert gpu["memory_reading_reliable"] is False


def test_hardware_stats_without_ops_controller_returns_no_gpus(monkeypatch):
    _ops_answers(monkeypatch, 503, {"detail": "connection refused"})
    stats = asyncio.run(app.hardware_stats())
    assert stats["gpu"] is None
    assert stats["gpus"] == []


def test_hardware_stats_on_a_host_without_gpus_returns_no_gpus(monkeypatch):
    _ops_answers(monkeypatch, 200, {"gpus": []})
    stats = asyncio.run(app.hardware_stats())
    assert stats["gpu"] is None
    assert stats["gpus"] == []
