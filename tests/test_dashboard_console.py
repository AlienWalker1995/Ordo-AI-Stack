"""Pure builders behind the dashboard's Overview, Services, Models and Media pages.

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

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import console  # noqa: E402

BIG = "GPU-97fe65ee"
SMALL = "GPU-20fac13a"
GPU_FILE = "Qwen3.8-27B-TurboFCFusion-Q6_K.gguf"
CPU_FILE = "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
EMBED_FILE = "nomic-embed-text-v1.5.Q4_K_M.gguf"
MMPROJ_FILE = "Qwen3.8-27B-TurboFable-vision-f16.gguf"

HW_GPUS = [
    {"index": 0, "uuid": SMALL, "name": "NVIDIA GeForce GTX 1070", "vram_total_gb": 8.6,
     "vram_used_gb": 3.0, "utilization_pct": 2, "temp_c": 42},
    {"index": 1, "uuid": BIG, "name": "NVIDIA GeForce RTX 5090", "vram_total_gb": 34.2,
     "vram_used_gb": 29.6, "utilization_pct": 0, "temp_c": 57},
]
REGISTRY = {
    "comfyui": {"id": "comfyui", "kind": "comfyui", "service": "comfyui", "gpu_uuid": BIG},
    "local-chat": {"id": "local-chat", "kind": "chat", "service": "llamacpp", "gpu_uuid": BIG,
                   "source": {"file": GPU_FILE}, "config": {"ctx": 106496, "mmproj": f"/models/{MMPROJ_FILE}"}},
    "local-embed": {"id": "local-embed", "kind": "embedding", "service": "llamacpp-embed", "gpu_uuid": BIG},
    "voice-stt": {"id": "voice-stt", "kind": "stt", "service": "stt", "gpu_uuid": SMALL},
    "voice-tts": {"id": "voice-tts", "kind": "tts", "service": "tts", "gpu_uuid": SMALL},
}
IDLE = {"state": "idle", "running": [], "queued": [], "evicted_residents": {}, "rejected": []}
RENDERING = {"state": "busy", "running": [{"id": "gate-comfyui", "kind": "media", "vram_gb": 31.8}],
             "queued": [], "evicted_residents": {"llamacpp": 27.5}, "rejected": []}


def containers(**states):
    rows = {
        "llamacpp": {"id": "llamacpp", "state": "running", "health": None, "status": "Up 3 hours"},
        "llamacpp-cpu": {"id": "llamacpp-cpu", "state": "running", "health": "healthy",
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
    assert console.lease_label("gate-comfyui") == "ComfyUI"
    assert console.lease_label("reel-render-42") == "reel-render-42"


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


# --- GPUs and the chat engine ---

def test_gpu_cards_list_tenants_per_card_biggest_first():
    cards = console.gpu_cards(HW_GPUS, REGISTRY, IDLE)
    assert [c["name"] for c in cards] == ["RTX 5090", "GTX 1070"]
    big, small = cards
    assert big["borrowed_by"] == []
    assert "llama.cpp" in big["tenants"] and "ComfyUI" in big["tenants"]
    assert small["tenants"] == ["Whisper", "Kokoro"]
    assert big["vram_used_gb"] == 29.6 and big["temp_c"] == 57


def test_a_render_shows_as_borrowing_the_resident_models_gpu():
    big = console.gpu_cards(HW_GPUS, REGISTRY, RENDERING)[0]
    assert big["borrowed_by"] == ["ComfyUI"]


def test_chat_runs_on_the_gpu_when_nothing_has_borrowed_it():
    assert console.chat_engine(IDLE, containers())["engine"] == "gpu"


def test_chat_falls_back_to_cpu_while_a_render_holds_the_gpu():
    engine = console.chat_engine(RENDERING, containers(llamacpp={"state": "exited", "status": "Exited (137) 1 minute ago"}))
    assert engine["engine"] == "cpu"
    assert "ComfyUI" in engine["reason"]


def test_chat_falls_back_to_cpu_when_the_gpu_server_is_down():
    engine = console.chat_engine(IDLE, containers(llamacpp={"state": "exited", "status": "Exited (1) 1 minute ago"}))
    assert engine["engine"] == "cpu"
    assert "not running" in engine["reason"]


def test_no_chat_engine_when_both_servers_are_down():
    engine = console.chat_engine(IDLE, containers(
        llamacpp={"state": "exited", "status": "Exited (1) now"},
        llamacpp_cpu={"state": "exited", "status": "Exited (1) now"},
    ))
    assert engine["engine"] == "none"


def test_unknown_control_plane_state_is_reported_not_guessed():
    assert console.chat_engine(None, {})["engine"] == "unknown"


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
    cards = [{"id": "llamacpp", "ops_service": "llamacpp", "name": "llama.cpp (GPU)", "ok": False,
              "error": "[Errno -2] Name or service not known"}]
    rows = containers(llamacpp={"state": "exited", "status": "Exited (137) 5 seconds ago"})
    items = console.build_attention(cards, rows, {"disk_pct": 10, "ram_pct": 10}, RENDERING)
    assert items == []
    # the same failure with no render holding the GPU is a real problem
    items = console.build_attention(cards, rows, {"disk_pct": 10, "ram_pct": 10}, IDLE)
    assert [i["title"] for i in items] == ["llama.cpp (GPU) is not responding"]


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


def test_media_summary_reports_the_running_job_and_queue_depth():
    queue = {"queue_running": [[5, "run-1", {}, {}, []]], "queue_pending": [[6, "p1", {}, {}, []], [7, "p2", {}, {}, []]]}
    media = console.summarise_media(queue, HISTORY, limit=2)
    assert media["running"] == {"prompt_id": "run-1"}
    assert media["pending"] == 2
    assert len(media["recent"]) == 2


def test_an_empty_queue_has_nothing_running():
    media = console.summarise_media({"queue_running": [], "queue_pending": []}, {}, limit=5)
    assert media == {"running": None, "pending": 0, "recent": []}


def test_activity_merges_sources_newest_first_and_drops_read_only_noise():
    leases = [
        {"id": "gate-comfyui", "kind": "media", "started": 1790171000.0, "ended": 1790171120.0, "outcome": "completed"},
        {"id": "train-lora", "kind": "train", "started": 1790170000.0, "ended": 1790170600.0, "outcome": "completed"},
    ]
    audit = [
        {"ts": 1790171500.0, "caller": "dashboard", "action": "restart", "target": "n8n", "result": "ok"},
        {"ts": 1790171600.0, "caller": "hermes", "action": "containers.list", "target": "*", "result": "ok"},
        {"ts": 1790171700.0, "caller": "hermes", "action": "diagnostics.dstate", "target": "", "result": "ok"},
    ]
    items = console.merge_activity(leases, audit, console.summarise_renders(HISTORY, 10), limit=10)
    titles = [i["title"] for i in items]
    assert titles[0].startswith("Render failed")
    assert any(t == "Restarted n8n" for t in titles)
    assert any("train-lora" in t for t in titles)
    assert not any("containers.list" in t or "dstate" in t for t in titles)
    assert not any(t.startswith("ComfyUI") for t in titles)  # gate leases are the renders themselves
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
    (_audit("start", "llamacpp", result="refused", status=409), "Started llamacpp (refused)"),
    (_audit("restart", "n8n", result="error", status=500), "Restarted n8n (failed)"),
])
def test_activity_titles_every_recorded_verb(entry, title):
    [item] = console.merge_activity([], [entry], [], limit=10)
    assert item["title"] == title
    assert item["severity"] == ("info" if entry["result"] == "ok" else "warning")


def test_activity_leaves_lease_calls_to_the_lease_history():
    # The lease history already shows each lease once; its request/heartbeat/release calls would
    # repeat it several times over.
    audit = [_audit("lease.request", "train-lora"), _audit("lease.heartbeat", "train-lora"),
             _audit("lease.release", "train-lora")]
    assert console.merge_activity([], audit, [], limit=10) == []


def test_activity_is_capped():
    audit = [{"ts": float(i), "caller": "dashboard", "action": "restart", "target": f"s{i}", "result": "ok"}
             for i in range(40)]
    assert len(console.merge_activity([], audit, [], limit=25)) == 25


# --- models ---

# The render's answer (ops-controller /model-config): every file a rendered service loads.
MODEL_FILES = [
    {"file": GPU_FILE, "service": "llamacpp", "optional": False},
    {"file": MMPROJ_FILE, "service": "llamacpp", "optional": True},
    {"file": CPU_FILE, "service": "llamacpp-cpu", "optional": False},
    {"file": EMBED_FILE, "service": "llamacpp-embed", "optional": False},
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
        {"file": GPU_FILE, "service": "llamacpp", "optional": False},
        {"file": "new-vision.gguf", "service": "llamacpp", "optional": True},
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


def test_a_switch_with_the_same_context_recreates_the_gpu_server_and_gateway():
    plan = console.switch_plan(106496, 106496)
    assert plan == {"recreate": ["llamacpp", "model-gateway"], "hermes_restart_needed": False}


def test_a_switch_that_changes_the_context_also_resizes_the_cpu_fallback_and_flags_hermes():
    plan = console.switch_plan(106496, 131072)
    assert plan["recreate"] == ["llamacpp", "model-gateway", "llamacpp-cpu"]
    assert plan["hermes_restart_needed"] is True


# --- services table ---

def test_service_table_groups_every_container_and_keeps_catalog_names():
    cards = [
        {"id": "webui", "name": "Open WebUI", "category": "interface", "ops_service": "open-webui",
         "open_url": "https://chat.example/", "ok": True, "error": None, "background": False},
        {"id": "llamacpp", "name": "llama.cpp (GPU)", "category": "inference", "ops_service": "llamacpp",
         "open_url": None, "ok": True, "error": None, "background": True},
    ]
    rows = {
        "open-webui": {"id": "open-webui", "state": "running", "health": "healthy", "status": "Up 1 hour (healthy)"},
        "llamacpp": {"id": "llamacpp", "state": "running", "health": None, "status": "Up 3 hours"},
        "mcp-searxng": {"id": "mcp-searxng", "state": "running", "health": "healthy", "status": "Up 2 days (healthy)"},
        "tailnet-chat": {"id": "tailnet-chat", "state": "running", "health": None, "status": "Up 2 days"},
        "evals": {"id": "evals", "state": "exited", "health": None, "status": "Exited (0) 3 hours ago"},
        "agent": {"id": "agent", "state": "running", "health": "healthy", "status": "Up 1 hour (healthy)"},
    }
    groups = {g["group"]: g["services"] for g in console.build_service_table(cards, rows)}
    webui = groups["Apps"][0]
    assert webui["name"] == "Open WebUI" and webui["verdict"] == "up" and webui["uptime"] == "1 hour"
    assert webui["open_url"] == "https://chat.example/" and webui["compose"] == "open-webui"
    assert groups["Inference"][0]["compose"] == "llamacpp"
    assert [s["compose"] for s in groups["Tools (MCP)"]] == ["mcp-searxng"]
    assert [s["compose"] for s in groups["Network"]] == ["tailnet-chat"]
    assert groups["Jobs"][0]["verdict"] == "done"
    agent = next(s for g in groups.values() for s in g if s["compose"] == "agent")
    assert agent["controllable"] is False  # the control plane refuses to cycle what serves it


def test_service_table_lists_a_catalog_card_whose_container_is_missing_as_stopped():
    cards = [{"id": "n8n", "name": "n8n", "category": "automation", "ops_service": "n8n",
              "open_url": None, "ok": False, "error": "container missing", "background": False}]
    groups = console.build_service_table(cards, {})
    assert groups[0]["services"][0]["verdict"] == "stopped"


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
    row = console.build_service_table(cards, rows)[0]["services"][0]
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
    cards = [{"id": "langfuse", "name": "Langfuse", "category": "observability", "ops_service": None,
              "open_url": "https://langfuse.example/", "ok": True, "error": None, "background": False}]
    row = console.build_service_table(cards, {})[0]["services"][0]
    assert row["verdict"] == "up"
    assert row["compose"] is None and row["card_id"] == "langfuse"
    assert row["controllable"] is False and row["actions"] == []


def test_a_failing_probe_only_card_is_unhealthy():
    cards = [{"id": "langfuse", "name": "Langfuse", "category": "observability", "ops_service": None,
              "open_url": None, "ok": False, "error": "HTTP 502", "background": False}]
    row = console.build_service_table(cards, {})[0]["services"][0]
    assert row["verdict"] == "unhealthy" and row["error"] == "HTTP 502"


def test_rows_carry_the_card_id_so_usage_keyed_by_card_can_be_matched():
    cards = [{"id": "webui", "name": "Open WebUI", "category": "interface", "ops_service": "open-webui",
              "open_url": None, "ok": True, "error": None, "background": False}]
    rows = {"open-webui": {"id": "open-webui", "state": "running", "health": None, "status": "Up 1 hour"}}
    row = console.build_service_table(cards, rows)[0]["services"][0]
    assert row["card_id"] == "webui" and row["compose"] == "open-webui"


def test_a_card_without_ops_service_uses_a_container_of_the_same_name_when_there_is_one():
    cards = [{"id": "couchdb", "name": "CouchDB (notes sync)", "category": "notes", "ops_service": None,
              "open_url": None, "ok": True, "error": None, "background": True}]
    rows = {"couchdb": {"id": "couchdb", "state": "running", "health": "healthy", "status": "Up 9 hours (healthy)"}}
    groups = console.build_service_table(cards, rows)
    all_rows = [r for g in groups for r in g["services"]]
    assert len(all_rows) == 1  # not listed twice, once as the card and once as a bare container
    assert all_rows[0]["compose"] == "couchdb" and all_rows[0]["controllable"] is True
