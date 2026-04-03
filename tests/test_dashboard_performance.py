from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def test_open_webui_default_model_prefers_chat_alias():
    import dashboard.app as dashboard_app

    assert dashboard_app._open_webui_default_model("qwen3-14b.gguf") == "qwen3-14b.gguf:chat"
    assert dashboard_app._open_webui_default_model("qwen3-14b.gguf:chat") == "qwen3-14b.gguf:chat"
    assert dashboard_app._open_webui_default_model("nomic-embed-text-v1.5.Q4_K_M.gguf") == "nomic-embed-text-v1.5.Q4_K_M.gguf"


def test_throughput_record_persists_ttft_in_summary():
    import dashboard.app as dashboard_app

    with dashboard_app._state_lock:
        dashboard_app._throughput_samples.clear()
        dashboard_app._ttft_samples.clear()
        dashboard_app._service_usage.clear()
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

    with patch.object(dashboard_app, "_AUTH_REQUIRED", False), \
         patch.object(dashboard_app, "rag_status", AsyncMock(return_value={"ok": True, "documents": 0})):
        summary = client.get("/api/performance/summary")

    assert summary.status_code == 200
    body = summary.json()
    top = body["throughput"]["top_models"][0]
    assert top["model"] == "qwen3-14b.gguf:chat"
    assert top["latest_ttft_ms"] == 180.0
    assert top["p95_ttft_ms"] == 180.0
