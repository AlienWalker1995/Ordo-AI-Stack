"""The dashboard's page endpoints: /api/overview, /api/activity, /api/services/table,
/api/models (+ switch, delete), /api/media (+ view) and /api/perf/*.

The fetchers are patched with payloads in the live shapes, so these pin the wiring and the
safety rules at the HTTP boundary: a model switch goes through the catalog and then recreates
exactly the services the plan names; a file a server is using cannot be deleted; the media
proxy only fetches output files; and a down dependency reads as unavailable, never as zeros.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import routes_console  # noqa: E402
from dashboard.app import app  # noqa: E402

GPU_FILE = "Qwen3.8-27B-TurboFCFusion-Q6_K.gguf"
CPU_FILE = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
EMBED_FILE = "nomic-embed-text-v1.5.Q4_K_M.gguf"
BIG, SMALL = "GPU-big", "GPU-small"

OPS = {
    "/status": {"gpu": {"state": "busy", "running": [{"id": "gate-comfyui", "kind": "media"}],
                        "queued": [], "evicted_residents": {"llamacpp": 27.5}, "rejected": []}},
    "/services": {"services": [
        {"id": "llamacpp", "state": "exited", "health": None, "status": "Exited (137) 1 minute ago"},
        {"id": "llamacpp-cpu", "state": "running", "health": "healthy", "status": "Up 3 hours (healthy)"},
        {"id": "open-webui", "state": "running", "health": "healthy", "status": "Up 1 hour (healthy)"},
        {"id": "evals", "state": "exited", "health": None, "status": "Exited (0) 3 hours ago"},
    ]},
    "/registry/models": {"models": {
        "local-chat": {"service": "llamacpp", "gpu_uuid": BIG, "source": {"file": GPU_FILE},
                       "config": {"mmproj": "/models/vision.gguf"}},
        "comfyui": {"service": "comfyui", "gpu_uuid": BIG},
        "voice-stt": {"service": "stt", "gpu_uuid": SMALL},
    }},
    "/model-config": {"active_model": "turbo", "active_file": GPU_FILE, "ctx_size": 106496,
                      "available": [{"id": "turbo", "tier": "ultra", "vram_gb": 24, "file": GPU_FILE},
                                    {"id": "absent", "tier": "ultra", "vram_gb": 24, "file": "absent.gguf"}]},
    "/jobs/history": {"history": [{"id": "train-lora", "kind": "train", "started": 100.0, "ended": 200.0,
                                   "outcome": "completed"}]},
    "/audit?limit=100": {"entries": [{"ts": 300.0, "caller": "dashboard", "action": "restart",
                                      "target": "n8n", "result": "ok"}]},
}
HARDWARE = {"cpu_pct": 18, "ram_used_gb": 46.8, "ram_total_gb": 109.7, "ram_pct": 43,
            "disk_used_gb": 1607.1, "disk_total_gb": 1999.8, "disk_pct": 80.4,
            "gpus": [{"uuid": SMALL, "name": "NVIDIA GeForce GTX 1070", "vram_total_gb": 8.6, "vram_used_gb": 3.0},
                     {"uuid": BIG, "name": "NVIDIA GeForce RTX 5090", "vram_total_gb": 34.2, "vram_used_gb": 29.6}]}
CARDS = [
    {"id": "webui", "name": "Open WebUI", "category": "interface", "ops_service": "open-webui",
     "open_url": "https://chat.example/", "ok": True, "error": None, "background": False},
    {"id": "codebase-memory", "name": "Codebase Memory", "category": "knowledge", "ops_service": "codebase-memory-ui",
     "open_url": "https://graph.example/", "ok": False, "error": "HTTP 404", "background": False},
]
SERVED = {"gpu": None, "cpu": CPU_FILE, "embed": EMBED_FILE}
DISK = [{"name": GPU_FILE, "size": 1}, {"name": CPU_FILE, "size": 2}, {"name": EMBED_FILE, "size": 3},
        {"name": "vision.gguf", "size": 4}, {"name": "spare.gguf", "size": 5}]
THROUGHPUT = {"models": {GPU_FILE: {"p50": 22.2}, CPU_FILE: {"p50": 2.7}}}
HISTORY = {"r1": {"status": {"status_str": "success", "messages": [["execution_success", {"timestamp": 400000}]]},
                  "outputs": {"9": {"audio": [{"filename": "song.mp3", "subfolder": "audio", "type": "output"}]}}}}


async def fake_ops_json(path):
    return OPS.get(path)


@pytest.fixture
def live():
    """Every fetcher patched with the payloads above."""
    with patch.object(routes_console, "_ops_json", side_effect=fake_ops_json), \
         patch.object(routes_console, "_hardware", new=AsyncMock(return_value=HARDWARE)), \
         patch.object(routes_console, "_service_cards", new=AsyncMock(return_value=CARDS)), \
         patch.object(routes_console, "_rag", new=AsyncMock(return_value={"ok": True, "points_count": 2483})), \
         patch.object(routes_console, "_throughput", new=AsyncMock(return_value=THROUGHPUT)), \
         patch.object(routes_console, "_served", new=AsyncMock(return_value=SERVED)), \
         patch.object(routes_console, "_disk_files", new=AsyncMock(return_value=DISK)), \
         patch.object(routes_console, "_comfy_json", new=AsyncMock(side_effect=lambda path: (
             {"queue_running": [], "queue_pending": []} if path == "/queue" else HISTORY))):
        yield TestClient(app)


# --- overview ---

def test_overview_says_chat_is_on_the_cpu_fallback_while_a_render_holds_the_gpu(live):
    body = live.get("/api/overview").json()
    assert body["chat"]["engine"] == "cpu"
    assert "ComfyUI" in body["chat"]["reason"]
    assert body["chat"]["cpu_p50"] == 2.7
    # the GPU model is evicted, so nothing reports it as loaded, but its speed is still known
    assert body["chat"]["gpu_p50"] == 22.2
    big = body["gpus"][0]
    assert big["name"] == "RTX 5090" and big["borrowed_by"] == ["ComfyUI"]


def test_overview_status_counts_what_needs_attention(live):
    body = live.get("/api/overview").json()
    titles = [a["title"] for a in body["attention"]]
    assert "Codebase Memory is not responding" in titles
    assert any(t.startswith("Disk 80%") for t in titles)
    assert not any("evals" in t for t in titles)
    assert body["status"]["level"] == "critical"
    assert body["status"]["text"] == "2 things need you"


def test_overview_carries_host_knowledge_and_links_from_the_catalog(live):
    body = live.get("/api/overview").json()
    assert body["host"]["disk_pct"] == 80.4 and body["knowledge"]["documents"] == 2483
    assert {"name": "Open WebUI", "url": "https://chat.example/"} in body["links"]


def test_overview_with_the_control_plane_down_is_unknown_not_healthy(live):
    with patch.object(routes_console, "_ops_json", new=AsyncMock(return_value=None)):
        body = TestClient(app).get("/api/overview").json()
    assert body["chat"]["engine"] == "unknown"
    assert body["status"]["level"] == "unknown"
    assert body["status"]["text"] == "The control plane is not answering"


# --- activity ---

def test_activity_merges_renders_leases_and_actions(live):
    items = live.get("/api/activity").json()["items"]
    assert [i["title"] for i in items] == ["Render finished · audio", "Restarted n8n", "GPU lease train-lora completed"]


# --- services ---

def test_service_table_is_grouped(live):
    groups = {g["group"]: g["services"] for g in live.get("/api/services/table").json()["groups"]}
    assert groups["Apps"][0]["name"] == "Open WebUI"
    assert groups["Jobs"][0]["compose"] == "evals" and groups["Jobs"][0]["verdict"] == "done"


# --- models ---

def test_models_page_lists_slots_catalog_and_files(live):
    body = live.get("/api/models").json()
    assert body["cpu"]["file"] == CPU_FILE
    catalog = {c["id"]: c for c in body["catalog"]}
    assert catalog["turbo"]["installed"] and not catalog["absent"]["installed"]
    files = {f["name"]: f["in_use"] for f in body["files"]}
    assert files["vision.gguf"] is True and files["spare.gguf"] is False


def test_switch_goes_through_the_catalog_then_recreates_what_the_plan_names(live):
    calls = []

    async def ops_call(method, path, json=None):
        calls.append((method, path, json))
        if path == "/model-config":
            return 200, {"ok": True, "active_model": "turbo", "ctx_size": 106496}
        # the control plane refuses a recreate that does not carry confirm (ordo/control.py)
        if not (json or {}).get("confirm"):
            return 400, {"error": "Destructive operation requires confirmation."}
        return 200, {"ok": True}

    with patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 200, r.text
    assert calls[0] == ("POST", "/model-config", {"model": "turbo"})
    assert [c[1] for c in calls[1:]] == ["/services/llamacpp/recreate", "/services/model-gateway/recreate"]
    assert r.json()["hermes_restart_needed"] is False


def test_switch_refuses_a_model_whose_file_is_not_downloaded(live):
    with patch.object(routes_console, "_ops_call", new=AsyncMock()) as ops_call:
        r = live.post("/api/models/switch", json={"model": "absent"})
    assert r.status_code == 409
    assert "not downloaded" in r.json()["detail"]
    ops_call.assert_not_called()


def test_switch_refuses_an_id_the_catalog_does_not_have(live):
    with patch.object(routes_console, "_ops_call", new=AsyncMock()) as ops_call:
        r = live.post("/api/models/switch", json={"model": "nope"})
    assert r.status_code == 404
    ops_call.assert_not_called()


def test_switch_stops_if_the_render_fails_and_recreates_nothing(live):
    calls = []

    async def ops_call(method, path, json=None):
        calls.append(path)
        return 500, {"error": "render failed"}

    with patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 502
    assert calls == ["/model-config"]


def test_delete_refuses_a_file_a_server_is_using(live, tmp_path):
    (tmp_path / CPU_FILE).write_bytes(b"x")
    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        r = live.post("/api/models/delete", json={"file": CPU_FILE})
    assert r.status_code == 409
    assert (tmp_path / CPU_FILE).exists()


def test_delete_removes_an_unused_file(live, tmp_path):
    (tmp_path / "spare.gguf").write_bytes(b"x")
    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        r = live.post("/api/models/delete", json={"file": "spare.gguf"})
    assert r.status_code == 200
    assert not (tmp_path / "spare.gguf").exists()


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b.gguf", "notes.txt", ""])
def test_delete_refuses_anything_that_is_not_a_bare_gguf_name(live, tmp_path, name):
    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        r = live.post("/api/models/delete", json={"file": name})
    assert r.status_code == 400


# --- media ---

def test_media_summarises_queue_and_recent_renders(live):
    body = live.get("/api/media").json()
    assert body["running"] is None and body["pending"] == 0
    assert body["recent"][0]["outputs"][0]["filename"] == "song.mp3"


@pytest.mark.parametrize("params", [
    {"filename": "../secret.png", "type": "output"},
    {"filename": "a.png", "type": "input"},
    {"filename": "a.png", "type": "output", "subfolder": "../x"},
    {"filename": "", "type": "output"},
])
def test_media_view_only_fetches_output_files(live, params):
    assert live.get("/api/media/view", params=params).status_code == 400


def test_media_view_proxies_a_valid_output(live):
    with patch.object(routes_console, "_comfy_bytes", new=AsyncMock(return_value=(b"PNG", "image/png"))):
        r = live.get("/api/media/view", params={"filename": "hero.png", "subfolder": "", "type": "output"})
    assert r.status_code == 200 and r.content == b"PNG" and r.headers["content-type"] == "image/png"


# --- performance ---

def test_perf_series_returns_gpu_and_cpu_rates(live):
    matrix = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"job": "llamacpp"}, "values": [[1000, "12.5"], [1300, "20"]]},
        {"metric": {"job": "llamacpp-cpu"}, "values": [[1000, "0"], [1300, "2.5"]]},
    ]}}
    with patch.object(routes_console, "_get_json", new=AsyncMock(return_value=matrix)):
        body = live.get("/api/perf/series").json()
    assert body["available"] is True
    assert body["gpu"] == [[1000, 12.5], [1300, 20.0]] and body["cpu"] == [[1000, 0.0], [1300, 2.5]]


def test_perf_series_without_prometheus_is_unavailable_not_empty(live):
    with patch.object(routes_console, "_get_json", new=AsyncMock(return_value=None)):
        body = live.get("/api/perf/series").json()
    assert body == {"available": False, "gpu": [], "cpu": []}


def test_grafana_status_reports_reachability_and_the_embed_path(live):
    with patch.object(routes_console, "_get_json", new=AsyncMock(return_value={"database": "ok"})):
        body = live.get("/api/perf/grafana").json()
    assert body["available"] is True and body["path"].startswith("/grafana/d/ordo-llm-gpu")
    with patch.object(routes_console, "_get_json", new=AsyncMock(return_value=None)):
        assert live.get("/api/perf/grafana").json()["available"] is False


def test_a_second_switch_while_one_is_running_is_refused(live):
    """Two switches interleaving would render one model and recreate for another."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ops_call(method, path, json=None):
        if path == "/model-config":
            started.set()
            await release.wait()
            return 200, {"active_model": "turbo", "ctx_size": 106496}
        return 200, {"ok": True}

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            first = asyncio.create_task(client.post("/api/models/switch", json={"model": "turbo"}))
            await started.wait()
            try:
                # Without a lock the second switch would also block on the render; bound it so
                # the missing lock shows up as a failure, not a hang.
                second = await asyncio.wait_for(
                    client.post("/api/models/switch", json={"model": "turbo"}), timeout=5)
                second_code = second.status_code
            except TimeoutError:
                second_code = "blocked (no lock)"
            release.set()
            return (await first).status_code, second_code

    with patch.object(routes_console, "_ops_call", side_effect=slow_ops_call):
        first_code, second_code = asyncio.run(scenario())
    assert first_code == 200
    assert second_code == 409
