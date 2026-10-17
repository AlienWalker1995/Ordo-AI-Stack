"""The dashboard's page endpoints: /api/overview, /api/activity, /api/services/table,
/api/models (+ switch, delete) and /api/perf/*.

The fetchers are patched with payloads in the live shapes, so these pin the wiring and the
safety rules at the HTTP boundary: a model switch goes through the catalog, and the control plane
recreates what the render changed; a file a server is using cannot be deleted; and a down
dependency reads as unavailable, never as zeros.
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
EMBED_FILE = "embeddinggemma-2-Q8_0.gguf"
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
    "/model-config": {"source_model": "auto", "active_model": "turbo", "active_file": GPU_FILE, "ctx_size": 106496,
                      "active_mmproj": "vision.gguf",
                      "model_files": [{"file": GPU_FILE, "service": "llamacpp", "optional": False},
                                      {"file": "vision.gguf", "service": "llamacpp", "optional": True},
                                      {"file": CPU_FILE, "service": "llamacpp-cpu", "optional": False},
                                      {"file": EMBED_FILE, "service": "llamacpp-embed", "optional": False}],
                      "available": [{"id": "turbo", "tier": "ultra", "vram_gb": 24, "file": GPU_FILE},
                                    {"id": "absent", "tier": "ultra", "vram_gb": 24, "file": "absent.gguf"}]},
    "/jobs/history": {"history": [{"id": "train-lora", "kind": "train", "started": 100.0, "ended": 200.0,
                                   "outcome": "completed"}]},
    "/audit?limit=100": {"entries": [{"ts": 300.0, "caller": "dashboard", "action": "restart",
                                      "target": "n8n", "result": "ok"}]},
    # The managed projects' own rows (ordo/control/managed_projects.py overview).
    "/projects": {"projects": [{"project": "media-stack", "containers": [
        {"name": "media-server", "service": "server", "state": "running", "health": "healthy",
         "status": "Up 4 days (healthy)", "image": "media"}]}]},
    "/doctor": {"ok": False, "checks": [
        {"check": "substrate", "ok": False, "detail": "substrate MISMATCH: ops-controller 79e3afd63f62 vs "
                                                      "the last render 0a898896c4f0."},
        {"check": "open-webui", "ok": True, "detail": "open-webui: not running"},
    ]},
}
# ops-controller /containers: every container on the host, a bare list (DockerBackend.container_row).
HOST_CONTAINERS = [
    {"name": "ordo-open-webui-1", "status": "running", "image": "webui", "project": "ordo", "service": "open-webui",
     "health": "healthy", "status_text": "Up 1 hour (healthy)"},
    {"name": "media-server", "status": "running", "image": "media", "project": "media-stack", "service": "server",
     "health": "healthy", "status_text": "Up 4 days (healthy)"},
    {"name": "web-site-web-1", "status": "running", "image": "nginx", "project": "web-site", "service": "web",
     "health": None, "status_text": "Up 6 days"},
]
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
THROUGHPUT = {"models": {GPU_FILE: {"p50": 22.2}, CPU_FILE: {"p50": 2.7}},
              "roles": {"gpu-chat": {"p50": 48.6, "n": 512, "first_ts": 1760000000,
                                     "last_ts": 1760086400, "model": GPU_FILE},
                        "cpu-fallback": {"p50": 2.7, "n": 3, "first_ts": 1760050000,
                                         "last_ts": 1760086000, "model": CPU_FILE}}}
HISTORY = {"r1": {"status": {"status_str": "success", "messages": [["execution_success", {"timestamp": 400000}]]},
                  "outputs": {"9": {"audio": [{"filename": "song.mp3", "subfolder": "audio", "type": "output"}]}}}}


async def fake_ops_json(path, timeout=None):
    return OPS.get(path)


IDLE_STATUS = {"gpu": {"state": "idle", "running": [], "queued": [], "evicted_residents": {}, "rejected": []}}


def gpu_idle():
    """The live fixture has a render holding the GPU; a switch needs it idle."""
    async def ops_json(path):
        return IDLE_STATUS if path == "/status" else OPS.get(path)
    return patch.object(routes_console, "_ops_json", side_effect=ops_json)


@pytest.fixture
def live(dashboard_operator_headers):
    """Every fetcher patched with the payloads above."""
    with patch.object(routes_console, "_ops_json", side_effect=fake_ops_json), \
         patch.object(routes_console, "_host_containers", new=AsyncMock(return_value=HOST_CONTAINERS)), \
         patch.object(routes_console, "_hardware", new=AsyncMock(return_value=HARDWARE)), \
         patch.object(routes_console, "_service_cards", new=AsyncMock(return_value=CARDS)), \
         patch.object(routes_console, "_rag", new=AsyncMock(return_value={"ok": True, "points_count": 2483})), \
         patch.object(routes_console, "_throughput", new=AsyncMock(return_value=THROUGHPUT)), \
         patch.object(routes_console, "_served", new=AsyncMock(return_value=SERVED)), \
         patch.object(routes_console, "_disk_files", new=AsyncMock(return_value=DISK)), \
         patch.object(routes_console, "_comfy_json", new=AsyncMock(side_effect=lambda path: (
             {"queue_running": [], "queue_pending": []} if path == "/queue" else HISTORY))):
        yield TestClient(app, headers=dashboard_operator_headers)


# --- overview ---

def test_overview_says_chat_is_on_the_cpu_fallback_while_a_render_holds_the_gpu(live):
    body = live.get("/api/overview").json()
    assert body["chat"]["engine"] == "cpu"
    assert "ComfyUI" in body["chat"]["reason"]
    # The medians come from the ROLE series: a model swap does not reset them, and each carries
    # its sample count (the page marks a thin one provisional).
    assert body["chat"]["cpu"]["p50"] == 2.7 and body["chat"]["cpu"]["n"] == 3
    assert body["chat"]["cpu"]["model"] == CPU_FILE
    # the GPU model is evicted, so nothing reports it as loaded, but its speed is still known
    assert body["chat"]["gpu"]["p50"] == 48.6 and body["chat"]["gpu"]["n"] == 512
    assert body["chat"]["gpu"]["model"] == GPU_FILE
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


def test_overview_service_count_leaves_out_finished_one_shot_jobs(live):
    """evals exited 0 (a finished job) and must not read as a service that is down; llamacpp,
    stopped by a GPU eviction, still counts against the total."""
    body = live.get("/api/overview").json()
    assert body["services"] == {"up": 2, "total": 3}


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


# --- drift (hostile audit OPS-3) ---

def test_drift_lists_the_checks_ordo_doctor_would_flag(live):
    body = live.get("/api/drift").json()
    assert body == {"available": True, "findings": [
        {"check": "substrate",
         "detail": "substrate MISMATCH: ops-controller 79e3afd63f62 vs the last render 0a898896c4f0."}]}


def test_drift_waits_for_the_probe_longer_than_a_plain_read(live):
    # GET /doctor runs the open-webui probe (two 15 s calls), so it gets more than _ops_json's default.
    seen = []

    async def ops_json(path, timeout=None):
        seen.append((path, timeout))
        return OPS.get(path)

    with patch.object(routes_console, "_ops_json", side_effect=ops_json):
        live.get("/api/drift")
    assert seen == [("/doctor", routes_console._DOCTOR_TIMEOUT)] and routes_console._DOCTOR_TIMEOUT > 30


def test_drift_with_the_control_plane_down_is_unavailable_not_clean(live):
    with patch.object(routes_console, "_ops_json", new=AsyncMock(return_value=None)):
        body = TestClient(app).get("/api/drift").json()
    assert body == {"available": False, "findings": []}


# --- activity ---

def test_activity_merges_renders_leases_and_actions(live):
    items = live.get("/api/activity").json()["items"]
    assert [i["title"] for i in items] == ["Render finished · audio", "Restarted n8n", "GPU lease train-lora completed"]


# --- services ---

def test_service_table_is_one_flat_list_of_every_container_on_the_host(live):
    table = live.get("/api/services/table").json()
    assert "groups" not in table and table["host_containers"] == "up"
    rows = {r["id"]: r for r in table["rows"]}
    assert rows["ordo:open-webui"]["name"] == "Open WebUI" and rows["ordo:open-webui"]["control"] == "ordo"
    assert rows["ordo:evals"]["verdict"] == "done"
    assert rows["host:media-server"]["control"] == "managed" and rows["host:media-server"]["actions"] == ["restart"]
    assert rows["host:web-site-web-1"]["control"] == "none"
    assert [r["project"] for r in table["rows"]][-2:] == ["media-stack", "web-site"]


def test_without_the_host_wide_list_the_table_says_so_and_keeps_the_ordo_rows(live):
    with patch.object(routes_console, "_host_containers", new=AsyncMock(return_value=None)):
        table = live.get("/api/services/table").json()
    assert table["host_containers"] == "down" and table["control_plane"] == "up"
    rows = {r["id"]: r for r in table["rows"]}
    assert "host:web-site-web-1" not in rows and "ordo:open-webui" in rows
    # The managed projects' own rows still list their containers.
    assert rows["host:media-server"]["control"] == "managed"


def test_a_model_evicted_for_a_render_cannot_be_started_from_the_table(live):
    # Starting it would put a second tenant on the leased GPU.
    rows = {r["compose"]: r for r in live.get("/api/services/table").json()["rows"] if r["compose"]}
    assert rows["llamacpp"]["lent"] is True
    assert rows["llamacpp"]["actions"] == []
    assert rows["llamacpp-cpu"]["lent"] is False



def test_with_the_control_plane_down_every_container_row_is_unknown_with_no_actions(live):
    # No container list is not "everything stopped": a Start offered on that guess is wrong.
    async def ops_down(path, timeout=None):
        return None

    with patch.object(routes_console, "_ops_json", side_effect=ops_down), \
         patch.object(routes_console, "_host_containers", new=AsyncMock(return_value=None)):
        table = live.get("/api/services/table").json()
    assert table["control_plane"] == "down"
    rows = [r for r in table["rows"] if r["compose"]]
    assert rows
    assert all(r["verdict"] == "unknown" and r["actions"] == [] for r in rows)


def test_with_the_control_plane_up_the_table_says_so(live):
    assert live.get("/api/services/table").json()["control_plane"] == "up"


class _OpsResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _OpsClient:
    """ops-controller answering every call with one response, or raising one exception."""

    def __init__(self, response=None, raises=None):
        self.response, self.raises, self.timeouts = response, raises, []

    async def request(self, method, url, headers=None, timeout=None, **_kwargs):
        self.timeouts.append(timeout)
        if self.raises:
            raise self.raises
        return self.response


OPS_LIFECYCLE_CALLS = [("post", "/api/ops/services/open-webui/start"),
                       ("post", "/api/ops/services/open-webui/stop"),
                       ("post", "/api/ops/services/open-webui/restart"),
                       ("get", "/api/ops/services/open-webui/logs"),
                       ("post", "/api/ops/projects/media-stack/containers/media-server/restart"),
                       ("get", "/api/ops/projects/media-stack/containers/media-server/logs")]


@pytest.mark.parametrize("method, url", OPS_LIFECYCLE_CALLS)
def test_an_ops_refusal_reaches_the_page_with_its_reason(method, url, monkeypatch, dashboard_operator_headers):
    # ops-controller answers {"error": ...}; the page shows `detail`, so the reason must land there.
    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _OpsClient(_OpsResponse(409, {"error": "another operation is in progress"}))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = getattr(TestClient(app, headers=dashboard_operator_headers), method)(url)
    assert r.status_code == 409
    assert r.json()["detail"] == "another operation is in progress"


def test_an_ops_answer_without_a_reason_is_passed_on_not_dropped(monkeypatch, dashboard_operator_headers):
    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _OpsClient(_OpsResponse(500, {"unexpected": 1}))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = TestClient(app, headers=dashboard_operator_headers).post("/api/ops/services/open-webui/restart")
    assert r.status_code == 500
    assert r.json()["detail"] == 'ops-controller answered HTTP 500: {"unexpected": 1}'


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_a_lifecycle_timeout_names_the_service_and_says_it_may_still_be_running(
        verb, monkeypatch, dashboard_operator_headers):
    import httpx

    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _OpsClient(raises=httpx.ReadTimeout(""))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = TestClient(app, headers=dashboard_operator_headers).post(f"/api/ops/services/open-webui/{verb}")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert f"/services/open-webui/{verb}" in detail
    assert "may still be running" in detail
    # A real restart takes 40-90 s; ops-controller's own budget is 60 s per docker verb.
    assert ops.timeouts == [dashboard_app._LIFECYCLE_TIMEOUT] and dashboard_app._LIFECYCLE_TIMEOUT >= 90


class _RecordingOpsClient(_OpsClient):
    """_OpsClient that also records each call's method, URL and JSON body."""

    def __init__(self, response):
        super().__init__(response)
        self.calls = []

    async def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return await super().request(method, url, headers=headers, timeout=timeout, **kwargs)


def test_a_managed_container_restart_is_confirmed_and_waits_like_a_lifecycle_call(
        monkeypatch, dashboard_operator_headers):
    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    answer = {"ok": True, "project": "media-stack", "container": "media-server", "action": "restarted"}
    ops = _RecordingOpsClient(_OpsResponse(200, answer))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = TestClient(app, headers=dashboard_operator_headers).post(
            "/api/ops/projects/media-stack/containers/media-server/restart")
    assert r.status_code == 200 and r.json() == answer
    method, url, body = ops.calls[0]
    assert method == "POST" and url.endswith("/projects/media-stack/containers/media-server/restart")
    assert body == {"confirm": True}
    assert ops.timeouts == [dashboard_app._LIFECYCLE_TIMEOUT]


def test_managed_container_logs_pass_the_tail_and_use_the_short_timeout(monkeypatch, dashboard_operator_headers):
    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _RecordingOpsClient(_OpsResponse(200, {"project": "media-stack", "container": "media-server",
                                                 "tail": 300, "logs": "ready"}))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = TestClient(app, headers=dashboard_operator_headers).get(
            "/api/ops/projects/media-stack/containers/media-server/logs?tail=300")
    assert r.json()["logs"] == "ready"
    assert ops.calls[0][1].endswith("/projects/media-stack/containers/media-server/logs?tail=300")
    assert ops.timeouts == [dashboard_app._LOGS_TIMEOUT]


@pytest.mark.parametrize("method, url", [("post", "/api/ops/projects/media-stack/containers/media-server/restart"),
                                         ("get", "/api/ops/projects/media-stack/containers/media-server/logs")])
def test_the_managed_container_routes_need_a_principal(method, url):
    r = getattr(TestClient(app), method)(url)
    assert r.status_code == 401


def test_logs_use_a_short_timeout(monkeypatch, dashboard_operator_headers):
    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _OpsClient(_OpsResponse(200, {"logs": "ready"}))
    with patch("dashboard.app._get_http_client", return_value=ops):
        r = TestClient(app, headers=dashboard_operator_headers).get("/api/ops/services/open-webui/logs")
    assert r.json() == {"logs": "ready"}
    assert ops.timeouts == [15.0]


# --- models ---

def test_models_page_lists_slots_catalog_and_files(live):
    body = live.get("/api/models").json()
    assert body["cpu"]["file"] == CPU_FILE
    catalog = {c["id"]: c for c in body["catalog"]}
    assert catalog["turbo"]["installed"] and not catalog["absent"]["installed"]
    files = {f["name"]: f["in_use"] for f in body["files"]}
    assert files["vision.gguf"] is True and files["spare.gguf"] is False


APPLIED = {"dry_run": False, "recreated": ["llamacpp", "llamacpp-cpu", "model-gateway"], "stopped": [],
           "restart_required_on_host": ["agent"], "host_command": "ordo apply --only agent",
           "host_reasons": {"agent": "agent runs the control plane"}}


def test_the_switch_is_one_control_plane_call_that_reports_what_the_render_changed(live):
    # The control plane writes the source, renders, and recreates exactly the changed set itself
    # (ordo/control/api.py apply_render): the dashboard keeps no list of its own.
    calls = []

    async def ops_call(method, path, json=None):
        calls.append((method, path, json))
        return 200, {"ok": True, "active_model": "turbo", "ctx_size": 131072, "apply": APPLIED}

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 200, r.text
    assert calls == [("POST", "/model-config", {"model": "turbo"})]
    body = r.json()
    assert body["recreated"] == ["llamacpp", "llamacpp-cpu", "model-gateway"]
    assert body["restart_required_on_host"] == ["agent"]
    assert body["host_command"] == "ordo apply --only agent"
    assert "hermes_restart_needed" not in body


def test_a_control_plane_that_did_not_apply_the_render_is_a_failure(live):
    # An ops-controller older than the post-render step renders and stops: nothing was recreated.
    async def ops_call(method, path, json=None):
        return 200, {"ok": True, "active_model": "turbo", "ctx_size": 131072}

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 502
    assert "ordo apply" in r.json()["detail"]


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

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 502
    assert calls == ["/model-config"]


def test_a_control_plane_refusal_passes_through_as_409_and_recreates_nothing(live):
    # The control plane refuses a model whose file (or projector) is missing from the volume.
    calls = []

    async def ops_call(method, path, json=None):
        calls.append(path)
        return 409, {"error": "vision.gguf is not in the ordo_models-gguf volume: run `ordo fetch turbo`"}

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 409
    assert "ordo fetch turbo" in r.json()["detail"]
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


def test_switch_is_refused_while_a_render_holds_the_gpu(live):
    # Recreating llama.cpp now would load the model onto a card a render has leased: two GPU
    # tenants at once is how the host crashed on 2026-08-08.
    with patch.object(routes_console, "_ops_call", new=AsyncMock()) as ops_call:
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 409
    assert "render" in r.json()["detail"]
    ops_call.assert_not_called()


def test_switch_is_refused_when_the_gpu_state_is_unknown(live):
    async def ops_json(path):
        return None if path == "/status" else OPS.get(path)

    with patch.object(routes_console, "_ops_json", side_effect=ops_json), \
         patch.object(routes_console, "_ops_call", new=AsyncMock()) as ops_call:
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 503
    ops_call.assert_not_called()


def test_a_failed_apply_reports_the_control_planes_rollback(live):
    # The control plane restores the previous source and re-applies it when the apply fails; the
    # dashboard passes that through and makes no second call of its own.
    calls = []

    async def ops_call(method, path, json=None):
        calls.append(path)
        return 500, {"error": "applying the render failed: docker said no; ordo.yaml and out/ were rolled "
                              "back and the previous render re-applied", "rolled_back": True}

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 502
    assert "rolled back" in r.json()["detail"]
    assert calls == ["/model-config"]


def test_a_lease_refusal_from_the_apply_passes_through_as_409(live):
    async def ops_call(method, path, json=None):
        return 409, {"error": "'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; ordo.yaml and "
                              "out/ were rolled back and the previous render re-applied", "rolled_back": True}

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=ops_call):
        r = live.post("/api/models/switch", json={"model": "turbo"})
    assert r.status_code == 409
    assert "evicted" in r.json()["detail"]


def test_delete_refuses_while_the_control_plane_is_unreadable(live, tmp_path):
    (tmp_path / "spare.gguf").write_bytes(b"x")

    async def ops_json(path):
        return None if path == "/model-config" else OPS.get(path)

    with patch.object(routes_console, "GGUF_DIR", tmp_path),          patch.object(routes_console, "_ops_json", side_effect=ops_json):
        r = live.post("/api/models/delete", json={"file": "spare.gguf"})
    assert r.status_code == 503
    assert (tmp_path / "spare.gguf").exists()


def test_delete_refuses_when_the_control_plane_does_not_list_the_rendered_files(live, tmp_path):
    # An ops-controller older than the render-derived answer: which files are in use is unknown.
    (tmp_path / "spare.gguf").write_bytes(b"x")
    older = {k: v for k, v in OPS["/model-config"].items() if k != "model_files"}

    async def ops_json(path):
        return older if path == "/model-config" else OPS.get(path)

    with patch.object(routes_console, "GGUF_DIR", tmp_path),          patch.object(routes_console, "_ops_json", side_effect=ops_json):
        r = live.post("/api/models/delete", json={"file": "spare.gguf"})
    assert r.status_code == 503
    assert (tmp_path / "spare.gguf").exists()


@pytest.mark.parametrize("name", [CPU_FILE, EMBED_FILE, "vision.gguf", GPU_FILE])
def test_delete_protects_every_rendered_file_while_no_server_answers(live, tmp_path, name):
    # A stopped, evicted or restarting server loads the render's file when it comes back.
    (tmp_path / name).write_bytes(b"x")
    with patch.object(routes_console, "GGUF_DIR", tmp_path),          patch.object(routes_console, "_served",
                      new=AsyncMock(return_value={"gpu": None, "cpu": None, "embed": None})):
        r = live.post("/api/models/delete", json={"file": name})
    assert r.status_code == 409
    assert (tmp_path / name).exists()


def test_delete_does_not_consult_the_retired_registry(live, tmp_path):
    (tmp_path / "spare.gguf").write_bytes(b"x")
    paths = []

    async def ops_json(path):
        paths.append(path)
        return OPS.get(path)

    with patch.object(routes_console, "GGUF_DIR", tmp_path),          patch.object(routes_console, "_ops_json", side_effect=ops_json):
        r = live.post("/api/models/delete", json={"file": "spare.gguf"})
    assert r.status_code == 200
    assert "/registry/models" not in paths


def test_delete_is_refused_while_a_switch_is_running(live, tmp_path, dashboard_operator_headers):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    (tmp_path / "spare.gguf").write_bytes(b"x")

    async def scenario():
        async with routes_console._switch_lock:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t", headers=dashboard_operator_headers
            ) as client:
                return (await client.post("/api/models/delete", json={"file": "spare.gguf"})).status_code

    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        assert asyncio.run(scenario()) == 409
    assert (tmp_path / "spare.gguf").exists()


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b.gguf", "notes.txt", ""])
def test_delete_refuses_anything_that_is_not_a_bare_gguf_name(live, tmp_path, name):
    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        r = live.post("/api/models/delete", json={"file": name})
    assert r.status_code == 400


def test_comfyui_calls_are_skipped_when_no_gate_url_is_configured(monkeypatch):
    """COMFYUI_URL is the gate's URL, rendered only while comfyui is enabled. Without it there is
    nowhere legitimate to send the request (comfyui itself is reachable only by its gate), so the
    dashboard reports ComfyUI as unavailable instead of guessing a direct address."""
    import asyncio

    monkeypatch.setattr(routes_console, "COMFYUI_URL", "")
    no_http = AsyncMock(side_effect=AssertionError("no request may be made without a gate URL"))
    with patch.object(routes_console, "_get_json", new=no_http):
        assert asyncio.run(routes_console._comfy_json("/history")) is None


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


def test_perf_series_reads_the_gpu_rate_from_the_gateway_not_llama_cpp(live):
    """NInfer serves no /metrics, so llama.cpp's counter left the GPU sparkline empty. The GPU
    series is the gateway's output-token rate for the llamacpp:8080 deployment (either engine)."""
    from urllib.parse import parse_qs, urlsplit

    fetch = AsyncMock(return_value={"status": "success", "data": {"result": []}})
    with patch.object(routes_console, "_get_json", new=fetch):
        live.get("/api/perf/series")
    query = parse_qs(urlsplit(fetch.call_args.args[0]).query)["query"][0]
    assert 'litellm_output_tokens_metric_total' in query and 'api_base="http://llamacpp:8080/v1"' in query
    assert 'llamacpp:tokens_predicted_total{job="llamacpp-cpu"}' in query


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


def test_a_second_switch_while_one_is_running_is_refused(live, dashboard_operator_headers):
    """Two switches interleaving would render one model and recreate for another."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ops_call(method, path, json=None):
        if path == "/model-config":
            started.set()
            await release.wait()
            return 200, {"active_model": "turbo", "ctx_size": 106496, "apply": APPLIED}
        return 200, {"ok": True}

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers=dashboard_operator_headers
        ) as client:
            first = asyncio.create_task(client.post("/api/models/switch", json={"model": "turbo"}))
            await asyncio.wait_for(started.wait(), timeout=5)  # a refused first switch fails, not hangs
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

    with gpu_idle(), patch.object(routes_console, "_ops_call", side_effect=slow_ops_call):
        first_code, second_code = asyncio.run(scenario())
    assert first_code == 200
    assert second_code == 409


def test_ops_requests_name_the_dashboard_as_the_actor(monkeypatch):
    # ops-controller's audit log records the X-Actor of every state-changing call.
    import asyncio

    from dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", "t")
    sent = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class _Client:
        async def request(self, method, url, headers=None, **_kwargs):
            sent.update(headers)
            return _Response()

    with patch("dashboard.app._get_http_client", return_value=_Client()):
        asyncio.run(dashboard_app._ops_request("POST", "/services/n8n/restart", json={"confirm": True}))
    assert sent["X-Actor"] == "dashboard"
    assert sent["Authorization"] == "Bearer t"


# --- NInfer artifacts: a .ninfer file is a GPU chat model like a GGUF ---

def test_the_model_scan_lists_ninfer_artifacts_beside_ggufs(tmp_path, monkeypatch):
    import dashboard.app as dashboard_app
    (tmp_path / "a.gguf").write_bytes(b"x")
    (tmp_path / "qwen3_8_27b_nvfp4.ninfer").write_bytes(b"xy")
    (tmp_path / "notes.txt").write_bytes(b"z")
    monkeypatch.setattr(dashboard_app, "_GGUF_MODELS_DIR", tmp_path)
    assert [m["name"] for m in dashboard_app._scan_gguf_models()] == ["a.gguf", "qwen3_8_27b_nvfp4.ninfer"]


def test_the_pin_alias_matches_the_gateway_for_both_engines():
    import dashboard.app as dashboard_app
    assert dashboard_app._gateway_pin_alias("Qwen3.8-27B-Q6_K.gguf") == "qwen3.8-27b-q6_k"
    assert dashboard_app._gateway_pin_alias("qwen3_8_27b_nvfp4.ninfer") == "qwen3_8_27b_nvfp4"


def test_a_ninfer_artifact_can_be_deleted_but_other_files_cannot(live, tmp_path):
    (tmp_path / "old.ninfer").write_bytes(b"x")
    with patch.object(routes_console, "GGUF_DIR", tmp_path):
        r = live.post("/api/models/delete", json={"file": "old.ninfer"})
        refused = live.post("/api/models/delete", json={"file": "old.bin"})
    assert r.status_code == 200, r.text
    assert refused.status_code == 400
