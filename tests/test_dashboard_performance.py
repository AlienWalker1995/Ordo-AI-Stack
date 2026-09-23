from __future__ import annotations

from fastapi.testclient import TestClient


def test_throughput_record_persists_ttft_in_stats():
    import dashboard.app as dashboard_app

    with dashboard_app._state_lock:
        dashboard_app._throughput_samples.clear()
        dashboard_app._ttft_samples.clear()
        dashboard_app._last_benchmark = None

    client = TestClient(dashboard_app.app)
    record = client.post(
        "/api/throughput/record",
        json={
            "model": "qwen3-14b.gguf:chat",
            "output_tokens_per_sec": 42.5,
            "service": "open-webui",
            "ttft_ms": 180.0,
        },
    )
    assert record.status_code == 200

    stats = client.get("/api/throughput/stats")
    assert stats.status_code == 200
    model = stats.json()["models"]["qwen3-14b.gguf:chat"]
    assert model["ttft_p50_ms"] == 180.0
    assert model["ttft_p95_ms"] == 180.0
