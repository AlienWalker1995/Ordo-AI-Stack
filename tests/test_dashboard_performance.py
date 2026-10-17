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
        dashboard_app._migration_in_progress = False
        dashboard_app._migration_next_try = 0.0
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


def test_v4_reload_restores_the_role_series(tmp_path, monkeypatch):
    """A v4 file's role series must land in the MODULE-LEVEL _role_samples. Without it in the
    global statement the assignment binds a local, every restart reloads into a throwaway dict,
    and the next debounced save overwrites the file with an empty role_samples."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    now = time.time()
    f.write_text(json.dumps({
        "version": 4,
        "samples": {"Qwen3.8-27B.gguf": [{"tps": 20.0, "ts": now - 100}]},
        "role_samples": {
            "gpu-chat": [{"tps": 20.0, "ts": now - 100, "model": "Qwen3.8-27B.gguf"}],
            "cpu-fallback": [{"tps": 8.0, "ts": now - 90, "model": "Qwen3.6-35B-A3B.gguf"}],
        },
        "ttft_samples": {},
        "last_benchmark": None,
    }))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    assert dashboard_app._migration_pending is False
    assert [s["tps"] for s in dashboard_app._role_samples["gpu-chat"]] == [20.0]
    assert [s["tps"] for s in dashboard_app._role_samples["cpu-fallback"]] == [8.0]
    # The live stats see the restored history instead of restarting from n=0.
    stats = asyncio.run(dashboard_app.throughput_stats())
    assert stats["roles"]["gpu-chat"]["n"] == 1 and stats["roles"]["gpu-chat"]["p50"] == 20.0


def test_samples_recorded_while_migration_waits_are_not_double_counted(tmp_path, monkeypatch):
    """While /model-config cannot be read, each new sample must reach the role series exactly
    once: when the migration later rebuilds it from the per-file series, not also by the direct
    append at record time (which would count it twice in the persisted store and in the live
    roles stats)."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    now = time.time()
    # A .ninfer artifact: the file-name guess, the render's mapping and the gateway's backend
    # all agree it is the GPU chat server's, so every path in this test lands in gpu-chat.
    f.write_text(json.dumps({
        "version": 3,
        "samples": {"qwen3.8-27b.ninfer": [{"tps": 20.0, "ts": now - 100}]},
        "ttft_samples": {},
        "last_benchmark": None,
    }))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    cfg = {"model_files": [{"file": "qwen3.8-27b.ninfer", "service": "llamacpp"}],
           "active_file": "qwen3.8-27b.ninfer"}
    ops = AsyncMock(return_value=(None, None))   # render down: the migration waits
    with patch.object(dashboard_app, "_ops_request", new=ops):
        client = TestClient(dashboard_app.app)
        for i in range(20):
            assert client.post("/api/throughput/record", json={
                "model": "qwen3.8-27b.ninfer", "output_tokens_per_sec": 30.0 + i,
                "alias": "local-chat", "backend": "llamacpp"}).status_code == 200
        assert dashboard_app._migration_pending is True
        # While pending the live roles view merges the per-file series on the fly: 21 samples,
        # none of them counted twice.
        stats = asyncio.run(dashboard_app.throughput_stats())
        assert stats["roles"]["gpu-chat"]["n"] == 21
    ops.return_value = (200, cfg)   # render back: the next record completes the migration
    dashboard_app._migration_next_try = 0.0   # skip the failure cooldown the waits above armed
    with patch.object(dashboard_app, "_ops_request", new=ops):
        client.post("/api/throughput/record", json={
            "model": "qwen3.8-27b.ninfer", "output_tokens_per_sec": 99.0,
            "alias": "local-chat", "backend": "llamacpp"})
    assert dashboard_app._migration_pending is False
    # 1 pre-existing + 20 recorded while waiting + 1 that triggered the migration = 22, each once.
    gpu = dashboard_app._role_samples["gpu-chat"]
    assert len(gpu) == 22
    assert len({s["tps"] for s in gpu}) == 22
    dashboard_app._save_throughput_state()
    data = json.loads(f.read_text())
    assert data["version"] == 4
    assert len(data["role_samples"]["gpu-chat"]) == 22
    assert len({s["tps"] for s in data["role_samples"]["gpu-chat"]}) == 22


def test_migration_trims_role_buckets_to_the_sample_cap(tmp_path, monkeypatch):
    """Several files classifying into one role must not push its bucket past
    _MAX_SAMPLES_PER_MODEL; the newest samples survive the trim."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    now = time.time()
    samples = {}
    for i in range(3):
        fname = f"model-{i}.gguf"
        samples[fname] = [{"tps": float(i * 1000 + j), "ts": now - 10000 + i * 1000 + j}
                          for j in range(300)]
    f.write_text(json.dumps({"version": 3, "samples": samples, "ttft_samples": {},
                             "last_benchmark": None}))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    cfg = {"model_files": [{"file": f"model-{i}.gguf", "service": "llamacpp-cpu"}
                           for i in range(3)],
           "active_file": "model-0.gguf"}
    with patch.object(dashboard_app, "_ops_request", new=AsyncMock(return_value=(200, cfg))):
        asyncio.run(dashboard_app._migrate_throughput_store())
    assert dashboard_app._migration_pending is False
    cpu = dashboard_app._role_samples["cpu-fallback"]
    assert len(cpu) == dashboard_app._MAX_SAMPLES_PER_MODEL
    assert cpu[-1]["model"] == "model-2.gguf"
    assert cpu[-1]["ts"] == now - 10000 + 2 * 1000 + 299


def test_a_migration_attempt_never_queues_callers_behind_it(tmp_path, monkeypatch):
    """One caller pays for the /model-config read; a concurrent one skips instead of waiting, and
    after a failed read nobody tries again until the cooldown has passed."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    f.write_text(json.dumps({"version": 3, "samples": {}, "ttft_samples": {}, "last_benchmark": None}))
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    _reset_store(dashboard_app)
    dashboard_app._load_throughput_state()
    ops = AsyncMock(return_value=(None, None))
    with patch.object(dashboard_app, "_ops_request", new=ops):
        # an attempt is in flight elsewhere: this caller must not touch the render at all
        dashboard_app._migration_in_progress = True
        asyncio.run(dashboard_app._maybe_migrate_throughput_store())
        assert ops.await_count == 0
        dashboard_app._migration_in_progress = False
        # the first free caller tries once and fails; the next one is inside the cooldown
        asyncio.run(dashboard_app._maybe_migrate_throughput_store())
        asyncio.run(dashboard_app._maybe_migrate_throughput_store())
        assert ops.await_count == 1
        assert dashboard_app._migration_pending is True
        # cooldown over: one more attempt
        dashboard_app._migration_next_try = 0.0
        asyncio.run(dashboard_app._maybe_migrate_throughput_store())
        assert ops.await_count == 2


def test_a_file_the_render_no_longer_names_gets_no_guessed_role():
    """A GGUF absent from /model-config could have served on either chat engine (a GPU model swapped
    out before the migration looks like a departed CPU model); it keeps its per-file series only,
    instead of landing GPU-speed samples under the CPU median."""
    import dashboard.app as dashboard_app
    cfg = {"model_files": [{"file": "current-cpu.gguf", "service": "llamacpp-cpu"}],
           "active_file": "active.ninfer", "available": []}
    assert dashboard_app._classify_file("current-cpu.gguf", cfg) == "cpu-fallback"
    assert dashboard_app._classify_file("active.ninfer", cfg) == "gpu-chat"
    assert dashboard_app._classify_file("old-whatever.gguf", cfg) is None
    roles: dict = {}
    dashboard_app._merge_into_roles(roles, {"old-whatever.gguf": [{"tps": 100.0, "ts": 1.0}],
                                            "current-cpu.gguf": [{"tps": 9.0, "ts": 2.0}]}, cfg)
    assert set(roles) == {"cpu-fallback"}
    assert [s["tps"] for s in roles["cpu-fallback"]] == [9.0]


def test_stats_survive_a_role_sample_without_a_model_name():
    import dashboard.app as dashboard_app
    _reset_store(dashboard_app)
    with dashboard_app._state_lock:
        dashboard_app._role_samples["gpu-chat"] = [{"tps": 40.0, "ts": time.time()}]
    with patch.object(dashboard_app, "_throughput_active_model", new=AsyncMock(return_value={"file": None, "ok": True, "reason": None, "last_sample_ts": None})):
        stats = asyncio.run(dashboard_app.throughput_stats())
    assert stats["roles"]["gpu-chat"]["n"] == 1
    assert stats["roles"]["gpu-chat"]["model"] is None
