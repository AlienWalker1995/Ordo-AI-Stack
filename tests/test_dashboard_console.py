"""Pure builders behind the dashboard's Overview, Services and Models pages.

Everything here takes plain dicts shaped like the live payloads (control-plane /status,
/services and /registry/models; the dashboard's hardware stats and throughput store; ComfyUI's
/queue and /history), so the behaviour that matters is pinned without any network:

* the GPU card says who holds each card, and when a render borrows the resident model's GPU
  the chat engine reads "cpu", not "down";
* a job that exited 0 is done, not failed, and never lands in "needs attention";
* model slots come from what each server actually loaded, catalog entries whose file is not on
  disk are marked not installed, and files in use cannot be deleted;
* activity merges renders, GPU leases and operator actions, newest first, without read-only noise.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import console  # noqa: E402

BIG = "GPU-97fe65ee"
SMALL = "GPU-20fac13a"
GPU_FILE = "Qwen3.8-27B-TurboFCFusion-Q6_K.gguf"
CPU_FILE = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
EMBED_FILE = "embeddinggemma-2-Q8_0.gguf"
MMPROJ_FILE = "Qwen3.8-27B-TurboFable-vision-f16.gguf"

HW_GPUS = [
    {"index": 0, "uuid": SMALL, "name": "NVIDIA GeForce GTX 1070", "vram_total_gb": 8.6,
     "vram_used_gb": 3.0, "utilization_pct": 2, "temp_c": 42},
    {"index": 1, "uuid": BIG, "name": "NVIDIA GeForce RTX 5090", "vram_total_gb": 34.2,
     "vram_used_gb": 29.6, "utilization_pct": 0, "temp_c": 57},
]
_MEASURED = {"format": "llamacpp", "path": "/metrics", "port": 8080}
_UNMEASURED = {"format": "none", "path": "/metrics", "port": None}
REGISTRY = {
    "comfyui": {"id": "comfyui", "kind": "image", "service": "comfyui", "gpu_uuid": BIG, "device": "gpu",
                "metrics": _UNMEASURED, "fallback_for": None},
    "local-chat": {"id": "local-chat", "kind": "chat", "service": "inference-engine-gpu", "gpu_uuid": BIG,
                   "device": "gpu", "metrics": _MEASURED, "fallback_for": None,
                   "source": {"file": GPU_FILE}, "config": {"ctx": 106496, "mmproj": f"/models/{MMPROJ_FILE}"}},
    "local-chat-cpu": {"id": "local-chat-cpu", "kind": "chat", "service": "inference-engine-cpu", "gpu_uuid": None,
                       "device": "cpu", "metrics": _MEASURED, "fallback_for": "local-chat",
                       "source": {"file": CPU_FILE}, "config": {"ctx": 200000}},
    "local-embed": {"id": "local-embed", "kind": "embedding", "service": "inference-engine-embed", "gpu_uuid": BIG,
                    "device": "gpu", "metrics": _UNMEASURED, "fallback_for": None},
    "voice-stt": {"id": "voice-stt", "kind": "stt", "service": "stt", "gpu_uuid": SMALL, "device": "gpu",
                  "metrics": _UNMEASURED, "fallback_for": None},
    "voice-tts": {"id": "voice-tts", "kind": "tts", "service": "tts", "gpu_uuid": SMALL, "device": "gpu",
                  "metrics": _UNMEASURED, "fallback_for": None},
}
# The service catalog's card names (console.service_names), as the dashboard shows them.
NAMES = {"comfyui": "ComfyUI", "inference-engine-gpu": "llama.cpp (GPU)"}
IDLE = {"state": "idle", "running": [], "queued": [], "idle_cached": {"inference-engine-gpu": 27.5},
        "evicted_residents": {}, "rejected": []}
RENDERING = {"state": "busy", "running": [{"id": "gate-comfyui", "kind": "media", "vram_gb": 31.8}],
             "queued": [], "idle_cached": {}, "evicted_residents": {"inference-engine-gpu": 27.5}, "rejected": []}


def containers(**states):
    rows = {
        "inference-engine-gpu": {"id": "inference-engine-gpu", "state": "running", "health": None, "status": "Up 3 hours"},
        "inference-engine-cpu": {"id": "inference-engine-cpu", "state": "running", "health": "healthy",
                         "status": "Up 3 hours (healthy)"},
    }
    for sid, row in states.items():
        rows[sid.replace("_", "-")] = {"id": sid.replace("_", "-"), **row}
    return rows


# --- small helpers ---

@pytest.mark.parametrize("raw, short", [
    ("NVIDIA GeForce RTX 5090", "RTX 5090"),
    ("NVIDIA GeForce GTX 1070", "GTX 1070"),
    ("Tesla T4", "Tesla T4"),
])
def test_gpu_names_drop_the_vendor_boilerplate(raw, short):
    assert console.short_gpu_name(raw) == short


def test_a_gate_lease_is_named_after_the_service_it_guards():
    assert console.lease_label("gate-comfyui") == "comfyui"
    assert console.lease_label("gate-comfyui", NAMES) == "ComfyUI"
    assert console.lease_label("reel-render-42", NAMES) == "reel-render-42"


def test_service_names_come_from_the_catalog_cards():
    cards = [{"id": "comfyui", "name": "ComfyUI"}, {"id": "gpu-card", "ops_service": "inference-engine-gpu",
                                                     "name": "NInfer (GPU)"}, {"id": "nameless"}]
    assert console.service_names(cards) == {"comfyui": "ComfyUI", "inference-engine-gpu": "NInfer (GPU)"}


@pytest.mark.parametrize("state, health, status, verdict", [
    ("running", None, "Up 3 hours", "up"),
    ("running", "healthy", "Up 3 hours (healthy)", "up"),
    ("running", "starting", "Up 5 seconds (health: starting)", "starting"),
    ("running", "unhealthy", "Up 2 hours (unhealthy)", "unhealthy"),
    ("exited", None, "Exited (0) 2 hours ago", "done"),
    ("exited", None, "Exited (1) 5 minutes ago", "failed"),
    ("exited", None, "Exited (137) 1 day ago", "stopped"),
    ("exited", None, "Exited (143) 1 day ago", "stopped"),
    ("created", None, "Created", "stopped"),
    ("restarting", None, "Restarting (1) 3 seconds ago", "failed"),
])
def test_container_verdicts_separate_finished_jobs_from_crashes(state, health, status, verdict):
    assert console.classify_container(state, health, status) == verdict


@pytest.mark.parametrize("status, uptime", [
    ("Up 3 hours (healthy)", "3 hours"),
    ("Up About an hour", "About an hour"),
    ("Up 5 seconds (health: starting)", "5 seconds"),
    ("Exited (0) 2 hours ago", None),
    ("", None),
])
def test_uptime_is_read_from_the_status_text(status, uptime):
    assert console.uptime_from_status(status) == uptime


# --- GPUs and the model engines ---

def test_gpu_cards_list_the_models_on_each_card_biggest_first():
    cards = console.gpu_cards(HW_GPUS, REGISTRY, IDLE)
    assert [c["name"] for c in cards] == ["RTX 5090", "GTX 1070"]
    big, small = cards
    assert big["borrowed_by"] == []
    assert big["tenants"] == ["comfyui", "local-chat", "local-embed"]
    assert small["tenants"] == ["voice-stt", "voice-tts"]
    assert big["vram_used_gb"] == 29.6 and big["temp_c"] == 57


def test_a_render_borrows_the_card_a_scheduled_resident_lives_on():
    big, small = console.gpu_cards(HW_GPUS, REGISTRY, RENDERING, NAMES)
    assert big["borrowed_by"] == ["ComfyUI"]
    assert small["borrowed_by"] == []      # no scheduled resident lives there


def _engines(status=IDLE, rates=None, **states):
    return {e["model"]: e for e in console.model_engines(REGISTRY, containers(**states), status, rates, NAMES)}


def test_every_registry_model_is_an_engine_chat_first():
    engines = console.model_engines(REGISTRY, containers(), IDLE, None, NAMES)
    assert [e["model"] for e in engines][:2] == ["local-chat", "local-chat-cpu"]
    assert {e["model"] for e in engines} == set(REGISTRY)
    chat = engines[0]
    assert chat["device"] == "gpu" and chat["file"] == GPU_FILE and chat["ctx"] == 106496
    assert chat["state"] == "serving" and chat["measured"] is True


def test_an_engine_on_a_lent_gpu_is_lent_not_down():
    engines = _engines(RENDERING, inference_engine_gpu={"state": "exited", "status": "Exited (137) 1 minute ago"})
    assert engines["local-chat"]["state"] == "lent"
    assert engines["local-chat"]["reason"] == "GPU lent to ComfyUI"
    assert engines["local-chat-cpu"]["standing_in"] is True


def test_a_crashed_engine_is_down():
    engines = _engines(inference_engine_gpu={"state": "exited", "status": "Exited (1) 1 minute ago"})
    assert engines["local-chat"]["state"] == "down"
    assert engines["local-chat-cpu"]["standing_in"] is True


def test_a_fallback_is_not_standing_in_while_the_model_it_covers_serves():
    assert _engines()["local-chat-cpu"]["standing_in"] is False


def test_speeds_are_attached_to_measured_engines_only():
    rates = {"inference-engine-gpu": {"generation_tps": 160.2}, "comfyui": {"output_tps": 9.0}}
    engines = _engines(rates=rates)
    assert engines["local-chat"]["speeds"] == {"generation_tps": 160.2}
    assert engines["comfyui"]["speeds"] is None          # not measured, whatever Prometheus says
    assert _engines(rates=None)["local-chat"]["speeds"] is None


def test_chat_is_answered_by_its_own_engine_when_it_serves():
    summary = console.chat_summary(console.model_engines(REGISTRY, containers(), IDLE, None), IDLE)
    assert summary["state"] == "primary" and summary["model"]["model"] == "local-chat"


def test_chat_is_answered_by_the_stand_in_while_the_gpu_is_lent():
    engines = console.model_engines(REGISTRY, containers(inference_engine_gpu={
        "state": "exited", "status": "Exited (137) now"}), RENDERING, None, NAMES)
    summary = console.chat_summary(engines, RENDERING)
    assert summary["state"] == "fallback" and summary["model"]["model"] == "local-chat-cpu"
    assert summary["reason"] == "GPU lent to ComfyUI"


def test_no_one_answers_chat_when_both_engines_are_down():
    engines = console.model_engines(REGISTRY, containers(
        inference_engine_gpu={"state": "exited", "status": "Exited (1) now"},
        inference_engine_cpu={"state": "exited", "status": "Exited (1) now"}), IDLE, None)
    assert console.chat_summary(engines, IDLE)["state"] == "none"


def test_unknown_control_plane_state_is_reported_not_guessed():
    assert console.chat_summary([], None)["state"] == "unknown"


def test_the_engine_list_names_no_service():
    """Engines come from the registry; console.py keeps no table of engine services."""
    source = (Path(console.__file__)).read_text(encoding="utf-8")
    for name in ("inference-engine-gpu", "inference-engine-cpu", "inference-engine-embed"):
        assert name not in source, f"console.py names {name}"


# --- attention ---

def test_attention_lists_real_problems_and_skips_finished_jobs():
    cards = [
        {"id": "codebase-memory", "name": "Codebase Memory", "ok": False, "error": "HTTP 404"},
        {"id": "webui", "name": "Open WebUI", "ok": True, "error": None},
        {"id": "worker", "name": "Worker", "ok": None, "error": None},
    ]
    rows = containers(
        evals={"state": "exited", "health": None, "status": "Exited (0) 3 hours ago"},
        qdrant={"state": "running", "health": "unhealthy", "status": "Up 2 hours (unhealthy)"},
        n8n={"state": "exited", "health": None, "status": "Exited (1) 1 minute ago"},
    )
    hardware = {"disk_pct": 80.4, "disk_used_gb": 1607.1, "disk_total_gb": 1999.8, "ram_pct": 41}
    items = console.build_attention(cards, rows, hardware, IDLE)
    titles = [i["title"] for i in items]
    assert "Codebase Memory is not responding" in titles
    assert "qdrant is unhealthy" in titles
    assert "n8n exited with an error" in titles
    assert not any("evals" in t for t in titles)
    assert any(i["title"].startswith("Disk 80%") and i["severity"] == "warning" for i in items)
    assert items[0]["severity"] == "critical"  # criticals sort first


def test_a_disk_below_the_threshold_is_not_an_alert():
    items = console.build_attention([], {}, {"disk_pct": 60, "ram_pct": 40}, IDLE)
    assert items == []


def test_a_rejected_gpu_job_is_an_alert():
    status = {**IDLE, "rejected": [{"id": "big-render", "reason": "needs 40 GB"}]}
    items = console.build_attention([], {}, {"disk_pct": 10, "ram_pct": 10}, status)
    assert items and "big-render" in items[0]["title"]


def test_a_model_evicted_for_a_render_is_not_an_alert():
    # While a render borrows the GPU the resident chat server is stopped on purpose: its probe
    # fails and its container is down, and the page already says chat is on the CPU fallback.
    cards = [{"id": "inference-engine-gpu", "ops_service": "inference-engine-gpu", "name": "llama.cpp (GPU)", "ok": False,
              "error": "[Errno -2] Name or service not known"}]
    rows = containers(inference_engine_gpu={"state": "exited", "status": "Exited (137) 5 seconds ago"})
    items = console.build_attention(cards, rows, {"disk_pct": 10, "ram_pct": 10}, RENDERING)
    assert items == []
    # the same failure with no render holding the GPU is a real problem
    items = console.build_attention(cards, rows, {"disk_pct": 10, "ram_pct": 10}, IDLE)
    assert [i["title"] for i in items] == ["llama.cpp (GPU) is not responding"]


def test_a_failed_host_container_is_critical_and_names_its_project():
    # The Overview feeds build_attention the table's rows outside the ordo project: a crashed
    # one needs a person, and the item says which stack it is in.
    rows = [r for r in _host_table() if r["project"] != console.ORDO_PROJECT]
    items = console.build_attention([], {}, {"disk_pct": 10, "ram_pct": 10}, IDLE, host_rows=rows)
    assert len(items) == 1
    assert items[0]["severity"] == "critical"
    assert items[0]["title"] == "media-indexer exited with an error"
    assert items[0]["detail"] == "media-stack: Exited (1) 2 hours ago"


def test_an_unhealthy_host_container_is_a_warning_that_names_its_project():
    rows = [{"name": "media-transcode", "project": "media-stack", "verdict": "unhealthy",
             "status": "Up 3 hours (unhealthy)"}]
    items = console.build_attention([], {}, {"disk_pct": 10, "ram_pct": 10}, IDLE, host_rows=rows)
    assert [(i["severity"], i["title"]) for i in items] == [("warning", "media-transcode is unhealthy")]
    assert items[0]["detail"] == "media-stack: Up 3 hours (unhealthy)"


def test_stopped_and_finished_host_containers_are_not_alerts():
    # Stopping is a choice and exit 0 is a finished job: neither needs a person.
    rows = [
        {"name": "web-site-stopped-1", "project": "web-site", "verdict": "stopped",
         "status": "Exited (137) 1 day ago"},
        {"name": "web-site-done-1", "project": "web-site", "verdict": "done",
         "status": "Exited (0) 1 day ago"},
    ]
    assert console.build_attention([], {}, {"disk_pct": 10, "ram_pct": 10}, IDLE, host_rows=rows) == []


# --- renders, media, activity ---

HISTORY = {
    "a1": {"status": {"status_str": "success", "messages": [["execution_success", {"timestamp": 1790171100000}]]},
           "outputs": {"9": {"audio": [{"filename": "song_00001.mp3", "subfolder": "audio", "type": "output"}]}}},
    "b2": {"status": {"status_str": "success", "messages": [["execution_success", {"timestamp": 1790171900000}]]},
           "outputs": {"7": {"images": [{"filename": "hero_00002.png", "subfolder": "", "type": "output"}]}}},
    "c3": {"status": {"status_str": "error", "messages": [["execution_error", {"timestamp": 1790172000000}]]},
           "outputs": {}},
}


def test_renders_are_newest_first_with_their_media_kind():
    renders = console.summarise_renders(HISTORY, limit=10)
    assert [r["prompt_id"] for r in renders] == ["c3", "b2", "a1"]
    assert renders[1]["kind"] == "image" and renders[1]["outputs"][0]["filename"] == "hero_00002.png"
    assert renders[2]["kind"] == "audio"
    assert renders[0]["ok"] is False and renders[0]["kind"] == "unknown"
    assert renders[1]["ts"] == 1790171900.0


def test_activity_merges_sources_newest_first_and_drops_read_only_noise():
    leases = [
        {"id": "gate-comfyui", "kind": "media", "started": 1790171000.0, "ended": 1790171120.0, "outcome": "completed"},
        {"id": "train-lora", "kind": "train", "started": 1790170000.0, "ended": 1790170600.0, "outcome": "completed"},
    ]
    audit = [
        {"ts": 1790171500.0, "caller": "dashboard", "action": "restart", "target": "n8n", "result": "ok"},
        {"ts": 1790171600.0, "caller": "hermes", "action": "containers.list", "target": "*", "result": "ok"},
        {"ts": 1790171700.0, "caller": "hermes", "action": "diagnostics.dstate", "target": "", "result": "ok"},
        # Reading another project's logs is audited, but it changes nothing.
        {"ts": 1790171750.0, "caller": "dashboard", "action": "project.logs", "target": "media/web", "result": "ok"},
    ]
    items = console.merge_activity(leases, audit, console.summarise_renders(HISTORY, 10), limit=10, names=NAMES)
    titles = [i["title"] for i in items]
    assert titles[0].startswith("Render failed")
    assert any(t == "Restarted n8n" for t in titles)
    assert any("train-lora" in t for t in titles)
    assert not any("containers.list" in t or "dstate" in t or "project.logs" in t for t in titles)
    assert "GPU lent to ComfyUI" in titles
    assert [i["ts"] for i in items] == sorted((i["ts"] for i in items), reverse=True)


def _audit(action, target="", **fields):
    return {"ts": 1790171500.0, "caller": "dashboard", "action": action, "target": target,
            "result": "ok", **fields}


@pytest.mark.parametrize("entry, title", [
    (_audit("plugin.enable", "comfyui"), "Enabled comfyui"),
    (_audit("plugin.disable", "comfyui"), "Disabled comfyui"),
    (_audit("compose.up", "open-webui"), "Compose up open-webui"),
    (_audit("compose.down", "open-webui"), "Compose down open-webui"),
    (_audit("compose.restart", "open-webui"), "Compose restart open-webui"),
    (_audit("model_config", "qwen"), "Switched model to qwen"),
    (_audit("models.download", "w.safetensors"), "Started download of w.safetensors"),
    (_audit("stop", "n8n", dry_run=True), "Stopped n8n (dry run)"),
    (_audit("start", "inference-engine-gpu", result="refused", status=409), "Started inference-engine-gpu (refused)"),
    (_audit("restart", "n8n", result="error", status=500), "Restarted n8n (failed)"),
])
def test_activity_titles_every_recorded_verb(entry, title):
    [item] = console.merge_activity([], [entry], [], limit=10, names=NAMES)
    assert item["title"] == title
    assert item["severity"] == ("info" if entry["result"] == "ok" else "warning")


def test_activity_leaves_lease_calls_to_the_lease_history():
    # The lease history already shows each lease once; its request/heartbeat/release calls would
    # repeat it several times over.
    audit = [_audit("lease.request", "train-lora"), _audit("lease.heartbeat", "train-lora"),
             _audit("lease.release", "train-lora")]
    assert console.merge_activity([], audit, [], limit=10, names=NAMES) == []


def test_activity_is_capped():
    audit = [{"ts": float(i), "caller": "dashboard", "action": "restart", "target": f"s{i}", "result": "ok"}
             for i in range(40)]
    assert len(console.merge_activity([], audit, [], limit=25, names=NAMES)) == 25


def test_gate_leases_are_visible_with_holder_and_duration():
    # The operator's ask: a render that borrowed the GPU must be readable afterwards, with who
    # held it, when, and for how long.
    leases = [{"id": "gate-comfyui", "kind": "media", "started": 1790171000.0,
               "ended": 1790171180.0, "outcome": "completed"}]
    [item] = console.merge_activity(leases, [], [], limit=10, names=NAMES)
    assert item["title"] == "GPU lent to ComfyUI"
    assert item["kind"] == "gpu"
    assert item["severity"] == "ok"
    assert item["holder"] == "ComfyUI"
    assert item["started"] == 1790171000.0
    assert item["ended"] == 1790171180.0
    assert item["duration_s"] == 180
    assert item["evicted"] == []


def test_rejected_gate_lease_reads_as_rejected_without_a_duration():
    # Rejected before it started: no start stamp, so no duration, and the row says why.
    leases = [{"id": "gate-comfyui", "kind": "media", "submitted": 100.0, "started": None,
               "ended": 200.0, "outcome": "rejected"}]
    [item] = console.merge_activity(leases, [], [], limit=10, names=NAMES)
    assert item["title"] == "GPU lent to ComfyUI (rejected)"
    assert item["severity"] == "warning"
    assert item["duration_s"] is None
    assert item["ts"] == 200.0


def test_non_gate_lease_keeps_its_title_and_carries_the_same_fields():
    leases = [{"id": "train-lora", "kind": "train", "started": 100.0, "ended": 700.0,
               "outcome": "completed"}]
    [item] = console.merge_activity(leases, [], [], limit=10, names=NAMES)
    assert item["title"] == "GPU lease train-lora completed"
    assert item["holder"] == "train-lora"
    assert item["duration_s"] == 600


def test_every_activity_item_carries_the_lease_fields():
    # One shape for every kind, so the page can read holder/duration without per-kind guards.
    audit = [_audit("restart", "n8n")]
    items = console.merge_activity([], audit, [], limit=10, names=NAMES)
    [item] = items
    assert set(item) == {"ts", "kind", "title", "severity", "holder", "started", "ended",
                         "duration_s", "evicted"}
    assert (item["holder"], item["started"], item["ended"], item["duration_s"]) == (None, None, None, None)
    assert item["evicted"] == []


@pytest.mark.parametrize("record, evicted", [
    ({"evicted": ["inference-engine-gpu"]}, ["llama.cpp (GPU)"]),   # the broker put the chat model away for it
    ({"evicted": ["inference-engine-gpu", "whisper"]}, ["llama.cpp (GPU)", "whisper"]),
    ({"evicted": []}, []),
    ({}, []),                                        # a record from before the broker kept this
])
def test_lease_says_which_residents_were_out_from_its_own_record(record, evicted):
    # The broker records which residents were out of the GPU as the lease started; the feed
    # reads that, never a guess from nearby restarts.
    leases = [{"id": "gate-comfyui", "kind": "media", "started": 1790171000.0,
               "ended": 1790171200.0, "outcome": "completed", **record}]
    restart_after = {**_audit("restart", "inference-engine-gpu"), "ts": 1790171300.0}
    items = console.merge_activity(leases, [restart_after], [], limit=10, names=NAMES)
    [item] = [i for i in items if i["kind"] == "gpu"]
    assert item["evicted"] == evicted


# --- models ---

# The render's answer (ops-controller /model-config): every file a rendered service loads.
MODEL_FILES = [
    {"file": GPU_FILE, "service": "inference-engine-gpu", "optional": False},
    {"file": MMPROJ_FILE, "service": "inference-engine-gpu", "optional": True},
    {"file": CPU_FILE, "service": "inference-engine-cpu", "optional": False},
    {"file": EMBED_FILE, "service": "inference-engine-embed", "optional": False},
]
MODEL_CONFIG = {
    "active_model": "qwen3.8-27b-turbo-fable-q6", "active_file": GPU_FILE, "ctx_size": 106496,
    "active_mmproj": MMPROJ_FILE, "model_files": MODEL_FILES,
    "available": [
        {"id": "qwen3.8-27b-turbo-fable-q6", "tier": "ultra", "vram_gb": 24.0, "file": GPU_FILE},
        {"id": "qwen3.8-27b-q6", "tier": "ultra", "vram_gb": 24.0, "file": "Qwen3.8-27B-Q6_K.gguf"},
    ],
}
DISK = [
    {"name": CPU_FILE, "size": 22_134_528_992},
    {"name": GPU_FILE, "size": 23_582_382_688},
    {"name": MMPROJ_FILE, "size": 928_000_000},
    {"name": EMBED_FILE, "size": 84_000_000},
    {"name": "old-experiment.gguf", "size": 5_000_000_000},
]
SERVED = {"gpu": GPU_FILE, "cpu": CPU_FILE, "embed": EMBED_FILE}
THROUGHPUT = {"models": {GPU_FILE: {"p50": 22.2, "sample_count": 500}, CPU_FILE: {"p50": 2.7, "sample_count": 217}}}


def test_model_slots_come_from_what_each_server_loaded():
    m = console.model_slots(MODEL_CONFIG, SERVED, DISK, THROUGHPUT)
    assert m["gpu"]["file"] == GPU_FILE and m["gpu"]["catalog_id"] == "qwen3.8-27b-turbo-fable-q6"
    assert m["gpu"]["p50"] == 22.2 and m["gpu"]["ctx"] == 106496
    assert m["cpu"]["file"] == CPU_FILE and m["cpu"]["p50"] == 2.7
    assert m["embed"]["file"] == EMBED_FILE


def test_catalog_entries_without_a_file_on_disk_are_not_installed():
    m = console.model_slots(MODEL_CONFIG, SERVED, DISK, THROUGHPUT)
    by_id = {c["id"]: c for c in m["catalog"]}
    assert by_id["qwen3.8-27b-turbo-fable-q6"]["installed"] is True
    assert by_id["qwen3.8-27b-turbo-fable-q6"]["active"] is True
    assert by_id["qwen3.8-27b-q6"]["installed"] is False


def test_files_in_use_by_any_server_are_marked_and_the_rest_are_not():
    m = console.model_slots(MODEL_CONFIG, SERVED, DISK, THROUGHPUT)
    used = {f["name"]: f["in_use"] for f in m["files"]}
    assert used[GPU_FILE] and used[CPU_FILE] and used[EMBED_FILE] and used[MMPROJ_FILE]
    assert used["old-experiment.gguf"] is False


def test_in_use_files_is_the_single_answer_for_delete_guards():
    assert console.in_use_files(SERVED, MODEL_CONFIG) == {GPU_FILE, CPU_FILE, EMBED_FILE, MMPROJ_FILE}


def test_every_file_the_render_loads_is_in_use_even_while_no_server_answers():
    """A server that is down, evicted or restarting loads the render's file when it comes back."""
    nothing_served = {"gpu": None, "cpu": None, "embed": None}
    assert console.in_use_files(nothing_served, MODEL_CONFIG) == {GPU_FILE, CPU_FILE, EMBED_FILE, MMPROJ_FILE}


def test_after_a_switch_the_new_projector_is_in_use_and_the_old_one_is_not():
    """The render names the projector the next load uses. The retired registry file kept naming
    the old one, which left the new projector deletable while llama.cpp was evicted."""
    switched = {**MODEL_CONFIG, "active_mmproj": "new-vision.gguf", "model_files": [
        {"file": GPU_FILE, "service": "inference-engine-gpu", "optional": False},
        {"file": "new-vision.gguf", "service": "inference-engine-gpu", "optional": True},
    ]}
    used = console.in_use_files({"gpu": None, "cpu": None, "embed": None}, switched)
    assert "new-vision.gguf" in used and MMPROJ_FILE not in used


def test_a_server_that_did_not_answer_and_has_no_declaration_leaves_its_slot_unknown():
    m = console.model_slots({**MODEL_CONFIG, "active_file": None},
                            {"gpu": None, "cpu": CPU_FILE, "embed": None}, DISK, THROUGHPUT)
    assert m["gpu"]["file"] is None and m["gpu"]["catalog_id"] == "qwen3.8-27b-turbo-fable-q6"


def test_served_file_is_the_basename_of_what_llama_server_reports():
    assert console.served_file({"data": [{"id": f"/models/{CPU_FILE}"}]}) == CPU_FILE
    assert console.served_file({"data": []}) is None
    assert console.served_file(None) is None


def test_the_dashboard_keeps_no_list_of_what_a_switch_restarts():
    # The control plane recreates exactly what the render changed (ordo/control/api.py apply_render).
    assert not hasattr(console, "switch_plan")


# --- services table ---

WEBUI_CARD = {"id": "webui", "name": "Open WebUI", "category": "interface", "ops_service": "open-webui",
              "open_url": "https://chat.example/", "ok": True, "error": None, "background": False}
PROBE_CARD = {"id": "langfuse", "name": "Langfuse", "category": "observability", "ops_service": None,
              "open_url": "https://langfuse.example/", "ok": True, "error": None, "background": False}
# ops-controller /services: the ordo project's containers, by compose service.
ORDO_SERVICES = {
    "open-webui": {"id": "open-webui", "name": "ordo-open-webui-1", "state": "running", "health": "healthy",
                   "status": "Up 1 hour (healthy)"},
    "mcp-searxng": {"id": "mcp-searxng", "name": "ordo-mcp-searxng-1", "state": "running", "health": "healthy",
                    "status": "Up 2 days (healthy)"},
    "evals": {"id": "evals", "name": "ordo-evals-1", "state": "exited", "health": None,
              "status": "Exited (0) 3 hours ago"},
    "agent": {"id": "agent", "name": "ordo-agent-1", "state": "running", "health": "healthy",
              "status": "Up 1 hour (healthy)"},
}
# ops-controller /containers: every container on the host, ordo's included (DockerBackend.container_row).
HOST_CONTAINERS = [
    {"name": "ordo-open-webui-1", "status": "running", "image": "webui", "project": "ordo", "service": "open-webui",
     "health": "healthy", "status_text": "Up 1 hour (healthy)"},
    {"name": "ordo-agent-1", "status": "running", "image": "agent", "project": "ordo", "service": "agent",
     "health": "healthy", "status_text": "Up 1 hour (healthy)"},
    {"name": "media-server", "status": "running", "image": "media", "project": "media-stack", "service": "server",
     "health": "healthy", "status_text": "Up 4 days (healthy)"},
    {"name": "media-indexer", "status": "exited", "image": "indexer", "project": "media-stack", "service": "indexer",
     "health": None, "status_text": "Exited (1) 2 hours ago"},
    {"name": "web-site-web-1", "status": "running", "image": "nginx", "project": "web-site", "service": "web",
     "health": None, "status_text": "Up 6 days"},
    {"name": "web-site-job-1", "status": "exited", "image": "busybox", "project": "web-site", "service": "job",
     "health": None, "status_text": "Exited (0) 1 day ago"},
    {"name": "adhoc", "status": "running", "image": "alpine", "project": "", "service": "", "health": None,
     "status_text": "Up 5 minutes"},
]
# ops-controller /projects: the managed projects' own rows (managed.ROW_FIELDS).
MANAGED = {"projects": [{"project": "media-stack", "containers": [
    {"name": "media-server", "service": "server", "state": "running", "health": "healthy",
     "status": "Up 4 days (healthy)", "image": "media"},
    {"name": "media-indexer", "service": "indexer", "state": "exited", "health": None,
     "status": "Exited (1) 2 hours ago", "image": "indexer"},
]}]}


def _host_table(**kwargs):
    args = {"host_containers": HOST_CONTAINERS, "managed": MANAGED, **kwargs}
    return console.build_service_table([WEBUI_CARD, PROBE_CARD], ORDO_SERVICES, **args)


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_service_table_lists_every_container_on_the_host_once():
    rows = _host_table()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == sorted([
        "ordo:open-webui", "link:langfuse", "ordo:mcp-searxng", "ordo:evals", "ordo:agent",
        "host:media-server", "host:media-indexer", "host:web-site-web-1", "host:web-site-job-1", "host:adhoc",
    ])
    # An ordo container is its ordo row, never a second host row as well.
    assert not any(r["container"] == "ordo-open-webui-1" and r["id"].startswith("host:") for r in rows)


def test_ordo_rows_keep_catalog_names_open_links_and_lifecycle_actions():
    webui = _by_id(_host_table())["ordo:open-webui"]
    assert webui["name"] == "Open WebUI" and webui["project"] == "ordo" and webui["compose"] == "open-webui"
    assert webui["container"] == "ordo-open-webui-1" and webui["card_id"] == "webui"
    assert webui["verdict"] == "up" and webui["uptime"] == "1 hour" and webui["open_url"] == "https://chat.example/"
    assert webui["control"] == "ordo" and webui["actions"] == ["restart", "stop"] and webui["reason"] is None
    evals = _by_id(_host_table())["ordo:evals"]
    assert evals["verdict"] == "done" and evals["actions"] == ["start"]


def test_each_row_says_which_routes_control_it():
    rows = _by_id(_host_table())
    assert {i: r["control"] for i, r in rows.items()} == {
        "ordo:open-webui": "ordo", "ordo:mcp-searxng": "ordo", "ordo:evals": "ordo",
        "ordo:agent": "host",  # the control plane refuses to cycle what serves it
        "link:langfuse": "link",
        "host:media-server": "managed", "host:media-indexer": "managed",
        "host:web-site-web-1": "none", "host:web-site-job-1": "none", "host:adhoc": "none",
    }
    assert rows["ordo:agent"]["actions"] == [] and rows["link:langfuse"]["actions"] == []


def test_a_managed_projects_container_offers_restart_while_it_runs():
    rows = _by_id(_host_table())
    server = rows["host:media-server"]
    assert server["name"] == "media-server" and server["project"] == "media-stack"
    assert server["container"] == "media-server" and server["compose"] is None
    assert server["verdict"] == "up" and server["uptime"] == "4 days" and server["actions"] == ["restart"]
    indexer = rows["host:media-indexer"]
    assert indexer["verdict"] == "failed" and indexer["actions"] == []  # no start verb for managed projects yet


def test_an_unmanaged_projects_container_is_read_only_and_says_how_to_manage_it():
    rows = _by_id(_host_table())
    web = rows["host:web-site-web-1"]
    assert web["verdict"] == "up" and web["uptime"] == "6 days" and web["actions"] == []
    assert web["reason"] == "add web-site to managed_projects in ordo.yaml"
    # docker's Status text gives the exit code: exit 0 is a finished job, not a failure.
    assert rows["host:web-site-job-1"]["verdict"] == "done"
    adhoc = rows["host:adhoc"]
    assert adhoc["project"] is None and adhoc["reason"] == "not started by compose, so no managed project can include it"


def test_without_the_managed_list_no_row_claims_its_project_is_unmanaged():
    server = _by_id(_host_table(managed=None))["host:media-server"]
    assert server["control"] == "none" and server["actions"] == []
    assert server["reason"] == "the control plane did not say which projects it manages"


def test_rows_sort_ordo_first_then_projects_alphabetically_then_name():
    rows = _host_table()
    assert [(r["project"], r["name"]) for r in rows] == [
        ("ordo", "agent"), ("ordo", "evals"), ("ordo", "Langfuse"), ("ordo", "mcp-searxng"), ("ordo", "Open WebUI"),
        ("media-stack", "media-indexer"), ("media-stack", "media-server"),
        ("web-site", "web-site-job-1"), ("web-site", "web-site-web-1"),
        (None, "adhoc"),
    ]


def test_with_the_control_plane_down_ordo_rows_are_unknown_with_no_actions():
    rows = console.build_service_table([WEBUI_CARD, PROBE_CARD], {}, control_plane_up=False)
    webui = _by_id(rows)["ordo:open-webui"]
    assert webui["verdict"] == "unknown" and webui["actions"] == [] and webui["control"] == "ordo"
    assert [r["id"] for r in rows] == ["link:langfuse", "ordo:open-webui"]


def test_the_table_has_no_groups():
    assert not hasattr(console, "_GROUP_ORDER")
    assert all("group" not in r for r in _host_table())


def test_service_counts_cover_every_project_and_leave_out_finished_jobs():
    # The Overview counts the table's rows: every container on the host, per project. evals and
    # web-site-job-1 finished (exit 0) and leave the totals; media-indexer crashed; the probe-only
    # card is not a container at all.
    counts = console.service_counts(_host_table())
    assert counts["up"] == 6 and counts["total"] == 7
    assert counts["by_project"] == {
        "ordo": {"total": 3, "up": 3, "failed": 0, "unhealthy": 0},
        "media-stack": {"total": 2, "up": 1, "failed": 1, "unhealthy": 0},
        "web-site": {"total": 1, "up": 1, "failed": 0, "unhealthy": 0},
        "": {"total": 1, "up": 1, "failed": 0, "unhealthy": 0},  # adhoc: compose did not start it
    }


def test_service_counts_keep_only_what_was_read_with_the_control_plane_down():
    # No /services answer: the ordo rows carry no container, so the counts say nothing about them
    # instead of guessing everything stopped.
    rows = console.build_service_table([WEBUI_CARD], {}, control_plane_up=False)
    assert console.service_counts(rows) == {"up": 0, "total": 0, "by_project": {}}


def test_the_dashboard_names_the_compose_project_the_stack_runs_as():
    from ordo.render.config import ORDO_PROJECT
    assert console.ORDO_PROJECT == ORDO_PROJECT


def test_service_table_lists_a_catalog_card_whose_container_is_missing_as_stopped():
    cards = [{"id": "n8n", "name": "n8n", "category": "automation", "ops_service": "n8n",
              "open_url": None, "ok": False, "error": "container missing", "background": False}]
    assert console.build_service_table(cards, {})[0]["verdict"] == "stopped"


@pytest.mark.parametrize("verdict, actions", [
    ("up", ["restart", "stop"]),
    ("starting", ["restart", "stop"]),
    ("unhealthy", ["restart", "stop"]),
    ("failed", ["start"]),
    ("stopped", ["start"]),
    ("done", ["start"]),
])
def test_actions_follow_the_state(verdict, actions):
    assert console.actions_for(verdict) == actions


def test_a_running_container_whose_probe_fails_is_unhealthy_not_up():
    cards = [{"id": "codebase-memory", "name": "Codebase Memory", "category": "knowledge",
              "ops_service": "codebase-memory-ui", "open_url": None, "ok": False,
              "error": "HTTP 404", "background": False}]
    rows = {"codebase-memory-ui": {"id": "codebase-memory-ui", "state": "running", "health": None,
                                   "status": "Up 2 days"}}
    row = console.build_service_table(cards, rows)[0]
    assert row["verdict"] == "unhealthy" and row["error"] == "HTTP 404"


def test_the_dashboard_hides_exactly_the_buttons_the_control_plane_refuses():
    from ordo.control.broker import DockerBackend
    assert console.NOT_CONTROLLABLE == DockerBackend.SELF_REFERENTIAL


def test_an_evicted_gpu_models_file_is_still_in_use():
    """During a render the GPU server is stopped, so it reports nothing loaded. Its file is still
    the active model and must not become deletable for the length of the render."""
    served_mid_render = {"gpu": None, "cpu": CPU_FILE, "embed": EMBED_FILE}
    assert GPU_FILE in console.in_use_files(served_mid_render, MODEL_CONFIG)


def test_the_gpu_slot_falls_back_to_the_rendered_model_while_the_server_is_evicted():
    m = console.model_slots(MODEL_CONFIG, {"gpu": None, "cpu": CPU_FILE, "embed": EMBED_FILE},
                            DISK, THROUGHPUT)
    assert m["gpu"]["file"] == GPU_FILE and m["gpu"]["p50"] == 22.2 and m["gpu"]["loaded"] is False


def test_a_probe_only_card_takes_its_state_from_the_probe_and_offers_no_controls():
    """Some cards are a link and a health probe, not one container (Langfuse is six coupled
    ones). Treating the card id as a container name called a healthy service 'stopped' and
    offered a Start button that could only fail."""
    row = console.build_service_table([PROBE_CARD], {})[0]
    assert row["verdict"] == "up"
    assert row["compose"] is None and row["card_id"] == "langfuse" and row["id"] == "link:langfuse"
    assert row["control"] == "link" and row["actions"] == []


def test_a_failing_probe_only_card_is_unhealthy():
    cards = [{**PROBE_CARD, "open_url": None, "ok": False, "error": "HTTP 502"}]
    row = console.build_service_table(cards, {})[0]
    assert row["verdict"] == "unhealthy" and row["error"] == "HTTP 502"


def test_rows_carry_the_card_id_so_usage_keyed_by_card_can_be_matched():
    rows = {"open-webui": {"id": "open-webui", "state": "running", "health": None, "status": "Up 1 hour"}}
    row = console.build_service_table([WEBUI_CARD], rows)[0]
    assert row["card_id"] == "webui" and row["compose"] == "open-webui"


def test_a_card_without_ops_service_uses_a_container_of_the_same_name_when_there_is_one():
    cards = [{"id": "couchdb", "name": "CouchDB (notes sync)", "category": "notes", "ops_service": None,
              "open_url": None, "ok": True, "error": None, "background": True}]
    rows = {"couchdb": {"id": "couchdb", "state": "running", "health": "healthy", "status": "Up 9 hours (healthy)"}}
    all_rows = console.build_service_table(cards, rows)
    assert len(all_rows) == 1  # not listed twice, once as the card and once as a bare container
    assert all_rows[0]["compose"] == "couchdb" and all_rows[0]["control"] == "ordo"


# The Hermes card as services/hermes-dashboard/catalog.json declares it: no lifecycle target of its
# own, but it runs inside the agent container.
HERMES_CARD = {"id": "hermes", "name": "Hermes Agent", "category": "agent", "ops_service": None, "runs_in": "agent",
               "open_url": "https://hermes.example/", "ok": True, "error": None, "background": False}


def test_a_card_that_runs_inside_a_container_is_one_row_with_that_container():
    rows = console.build_service_table([HERMES_CARD], ORDO_SERVICES)
    hermes = [r for r in rows if r["card_id"] == "hermes" or r["compose"] == "agent"]
    assert len(hermes) == 1  # not a link row plus a bare `agent` row
    assert {k: hermes[0][k] for k in ("id", "name", "compose", "container", "open_url", "verdict", "uptime",
                                      "control", "actions")} == {
        "id": "ordo:agent", "name": "Hermes Agent", "compose": "agent", "container": "ordo-agent-1",
        "open_url": "https://hermes.example/", "verdict": "up", "uptime": "1 hour", "control": "host", "actions": [],
    }


def test_the_hermes_card_names_the_container_it_runs_in():
    from dashboard.services_catalog import SERVICES
    card = next(c for c in SERVICES if c["id"] == "hermes")
    assert card["runs_in"] == "agent" and not card.get("ops_service")


# --- drift: ops-controller's GET /doctor, the checks `ordo doctor` would flag (hostile audit OPS-3) ---

def test_drift_keeps_only_the_failed_checks():
    doctor = {"ok": False, "checks": [
        {"check": "substrate", "ok": True, "detail": "substrate: ops-controller matches the last render (abc)"},
        {"check": "open-webui", "ok": False, "detail": "open-webui: model-gateway refuses the chat connection"},
    ]}
    assert console.drift(doctor) == {"available": True, "findings": [
        {"check": "open-webui", "detail": "open-webui: model-gateway refuses the chat connection"}]}


def test_no_failed_check_is_no_drift():
    assert console.drift({"ok": True, "checks": [{"check": "substrate", "ok": True, "detail": "x"}]}) ==         {"available": True, "findings": []}


@pytest.mark.parametrize("doctor", [None, [], "error"])
def test_an_unanswered_doctor_is_unavailable_not_clean(doctor):
    assert console.drift(doctor) == {"available": False, "findings": []}


def test_slot_medians_say_whether_they_are_provisional():
    """The threshold lives in one place: the backend decides, the page only renders the flag."""
    tp = {"models": {"a.gguf": {"p50": 48.6, "sample_count": 512}, "b.gguf": {"p50": 9.2, "sample_count": 3}}}
    slots = console.model_slots({"active_file": "a.gguf"}, {"gpu": "a.gguf", "cpu": "b.gguf", "embed": None}, [], tp)
    assert slots["gpu"]["provisional"] is False and slots["cpu"]["provisional"] is True
