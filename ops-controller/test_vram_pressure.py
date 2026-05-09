"""Tests for Phase 1 (post-drain /free) and Phase 2 (VRAM-pressure watchdog).

The orchestration loops themselves are long-running and hard to drive in unit
tests; we test the pure helpers (_call_comfyui_free, _read_total_vram_used_gb)
which carry the load-bearing behavior. The loops just thread polling around them.
"""
from __future__ import annotations

import json

# ── _call_comfyui_free ───────────────────────────────────────────────────────


def test_call_comfyui_free_ok_on_200(monkeypatch):
    import ops_controller.main as m

    captured: dict = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    ok, detail = m._call_comfyui_free(reason="post_drain")
    assert ok is True
    assert "http=200" in detail
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/free")
    body = json.loads(captured["body"])
    assert body == {"unload_models": True, "free_memory": True}


def test_call_comfyui_free_includes_reason_in_detail(monkeypatch):
    import ops_controller.main as m

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp())
    _, detail = m._call_comfyui_free(reason="pressure_breach")
    assert "pressure_breach" in detail


def test_call_comfyui_free_fail_on_exception(monkeypatch):
    import ops_controller.main as m

    def _raise(req, timeout):
        raise ConnectionError("comfy unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ok, detail = m._call_comfyui_free()
    assert ok is False
    assert "ConnectionError" in detail
    assert "comfy unreachable" in detail


def test_call_comfyui_free_fail_on_5xx(monkeypatch):
    import ops_controller.main as m

    class _Resp:
        status = 503

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Resp())
    ok, detail = m._call_comfyui_free()
    assert ok is False
    assert "http=503" in detail


# ── _read_total_vram_used_gb ─────────────────────────────────────────────────


def test_read_total_vram_used_gb_returns_used(monkeypatch):
    import ops_controller.main as m

    monkeypatch.setattr(
        m, "_nvml_vraam_by_pid",
        lambda: ({}, {"total_gb": 32.0, "used_gb": 27.5, "utilization_pct": 90, "per_pid_available": True}),
    )
    assert m._read_total_vram_used_gb() == 27.5


def test_read_total_vram_used_gb_none_when_nvml_unavailable(monkeypatch):
    import ops_controller.main as m

    # NVML unavailable shape: total_gb=0.0, used_gb=0.0, per_pid_available=False
    monkeypatch.setattr(
        m, "_nvml_vraam_by_pid",
        lambda: ({}, {"total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}),
    )
    assert m._read_total_vram_used_gb() is None


def test_read_total_vram_used_gb_zero_when_idle_but_available(monkeypatch):
    """A real card with 0 used (rare but possible) should return 0.0, not None."""
    import ops_controller.main as m

    monkeypatch.setattr(
        m, "_nvml_vraam_by_pid",
        lambda: ({}, {"total_gb": 32.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": True}),
    )
    assert m._read_total_vram_used_gb() == 0.0
