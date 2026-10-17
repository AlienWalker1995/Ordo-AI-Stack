from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

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

    stats = asyncio.run(dashboard_app.throughput_stats())
    model = stats["models"]["qwen3-14b.gguf:chat"]
    assert model["ttft_p50_ms"] == 180.0
    assert model["ttft_p95_ms"] == 180.0


def _reset_store(dashboard_app):
    with dashboard_app._state_lock:
        dashboard_app._throughput_samples.clear()
        dashboard_app._ttft_samples.clear()
        dashboard_app._role_samples.clear()
        dashboard_app._last_benchmark = None
        dashboard_app._migration_pending = False
    # Force the next debounced save to fire, so assertions on the persisted file are deterministic.
    dashboard_app._throughput_last_save = 0.0


def test_role_classification_prefers_the_serving_backend():
    """Which server answered names the role; alias and file name are fallbacks for an old gateway."""
    import dashboard.app as dashboard_app
    assert dashboard_app._role_for("Qwen3.8-27B.gguf", "local-chat", "llamacpp") == "gpu-chat"
    assert dashboard_app._role_for("Qwen3.6-35B-A3B.gguf", "local-chat", "llamacpp-cpu") == "cpu-fallback"
    assert dashboard_app._role_for("embeddinggemma-2-Q8_0.gguf", "local-embed", "") == "embed"
    assert dashboard_app._role_for("Qwen3.8-27B.gguf", "local-chat-cpu", "") == "cpu-fallback"
    # No signal at all: the primary engine.
    assert dashboard_app._role_for("Qwen3.6-35B-A3B.gguf", "", "") == "gpu-chat"


def test_record_stores_the_sample_under_the_role_that_served_it():
    import dashboard.app as dashboard_app
    _reset_store(dashboard_app)
    client = TestClient(dashboard_app.app)
    record = client.post(
        "/api/throughput/record",
        json={"model": "Qwen3.6-35B-A3B.gguf", "output_tokens_per_sec": 9.1,
              "alias": "local-chat", "backend": "llamacpp-cpu"},
    )
    assert record.status_code == 200
    stats = asyncio.run(dashboard_app.throughput_stats())
    role = stats["roles"]["cpu-fallback"]
    assert role["n"] == 1 and role["p50"] == 9.1
    assert role["model"] == "Qwen3.6-35B-A3B.gguf"
    assert stats["models"]["Qwen3.6-35B-A3B.gguf"]["sample_count"] == 1


def test_v3_store_migrates_to_roles_and_persists_v4(tmp_path, monkeypatch):
    """A v3 file's per-file series is assigned to the roles the render served them under, then
    rewritten as v4; while /model-config cannot be read the migration waits, never guesses."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    now = time.time()
    f.write_text(json.dumps({
        "version": 3,
        "samples": {"Qwen3.8-27B.gguf": [{"tps": 20.0, "ts": now - 100}],
                    "Qwen3.6-35B-A3B.gguf": [{"tps": 8.0, "ts": now - 90}]},
        "ttft_samples": {},
        "last_benchmark": None,
    }))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    assert dashboard_app._migration_pending is True

    cfg = {"model_files": [{"file": "Qwen3.8-27B.gguf", "service": "llamacpp"},
                           {"file": "Qwen3.6-35B-A3B.gguf", "service": "llamacpp-cpu"}],
           "active_file": "Qwen3.8-27B.gguf"}
    with patch.object(dashboard_app, "_ops_request", new=AsyncMock(return_value=(200, cfg))):
        client = TestClient(dashboard_app.app)
        assert client.post("/api/throughput/record", json={
            "model": "Qwen3.8-27B.gguf", "output_tokens_per_sec": 30.0,
            "alias": "local-chat", "backend": "llamacpp"}).status_code == 200
    data = json.loads(f.read_text())
    assert data["version"] == 4
    assert {s["tps"] for s in data["role_samples"]["gpu-chat"]} == {20.0, 30.0}
    migrated = data["role_samples"]["cpu-fallback"][0]
    assert migrated["tps"] == 8.0 and migrated["model"] == "Qwen3.6-35B-A3B.gguf"
    assert dashboard_app._migration_pending is False


def test_migration_waits_while_the_render_config_cannot_be_read(tmp_path, monkeypatch):
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    f.write_text(json.dumps({"version": 3, "samples": {}, "ttft_samples": {},
                             "last_benchmark": None}))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    with patch.object(dashboard_app, "_ops_request", new=AsyncMock(return_value=(None, None))):
        asyncio.run(dashboard_app._migrate_throughput_store())
    assert dashboard_app._migration_pending is True
    assert not f.read_text().startswith('{"version": 4')


def test_eviction_prunes_the_role_series_too():
    import dashboard.app as dashboard_app
    _reset_store(dashboard_app)
    now = time.time()
    with dashboard_app._state_lock:
        dashboard_app._role_samples["gpu-chat"] = [
            {"tps": 10.0, "ts": now - 8 * 86400, "model": "old.gguf"},
            {"tps": 20.0, "ts": now - 60, "model": "new.gguf"},
        ]
        dashboard_app._evict_stale_models(now)
    assert [s["model"] for s in dashboard_app._role_samples["gpu-chat"]] == ["new.gguf"]
