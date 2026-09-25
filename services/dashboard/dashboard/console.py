"""Pure builders for the dashboard's Overview, Services, Models and Media pages.

Every function here takes plain dicts in the shapes the live sources return (control-plane
/status, /services and /registry/models; the dashboard's own hardware stats and throughput
store; ComfyUI's /queue and /history; llama-server's /v1/models) and returns what a page
renders. No I/O: routes_console.py fetches, these decide. That split is what lets the rules
that matter (a render borrowing the GPU is not an outage, a finished job is not a failure, an
in-use model file cannot be deleted) be tested without a running stack.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# Services the control plane refuses to cycle because they serve the request itself. Mirrors
# ordo.control.broker.DockerBackend.SELF_REFERENTIAL (a repo test keeps the two equal); the dashboard
# only uses it to hide buttons that would always be refused.
NOT_CONTROLLABLE = frozenset({"agent", "ops-controller"})

# How a registry entry's service reads on a GPU card.
_TENANT_LABELS = {
    "llamacpp": "llama.cpp",
    "llamacpp-embed": "Embeddings",
    "comfyui": "ComfyUI",
    "stt": "Whisper",
    "tts": "Kokoro",
}

# Audit actions that only read state. They are most of the log (Hermes polls) and none of them
# is something a person would want in an activity feed.
_READ_ONLY_ACTIONS = frozenset({
    "containers.list", "container.logs", "diagnostics.dstate", "logs",
})
# GPU lease calls. The lease history already shows each lease once; its request, heartbeat and
# release calls would repeat it several times over.
_LEASE_ACTIONS = frozenset({"lease.request", "lease.heartbeat", "lease.release"})

_AUDIT_TITLES = {
    "restart": "Restarted {target}",
    "container.restart": "Restarted {target}",
    "start": "Started {target}",
    "stop": "Stopped {target}",
    "recreate": "Recreated {target}",
    "env_set": "Changed {target}",
    "model_config": "Switched model to {target}",
    "model_switch": "Switched model to {target}",
    "pull": "Pulled image {target}",
    "comfyui_pip_install": "Installed requirements for {target}",
    "model_delete": "Deleted model file {target}",
    "plugin.enable": "Enabled {target}",
    "plugin.disable": "Disabled {target}",
    "compose.up": "Compose up {target}",
    "compose.down": "Compose down {target}",
    "compose.restart": "Compose restart {target}",
    "models.download": "Started download of {target}",
}
# How a call that did not succeed reads, by the audit record's `result`.
_AUDIT_OUTCOME_SUFFIX = {"refused": " (refused)", "error": " (failed)"}

_MEDIA_BY_SUFFIX = {
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp"), "image"),
    **dict.fromkeys((".mp4", ".webm", ".mov", ".gif", ".mkv"), "video"),
    **dict.fromkeys((".mp3", ".wav", ".flac", ".ogg", ".m4a"), "audio"),
    **dict.fromkeys((".glb", ".obj", ".ply", ".stl"), "3d"),
}

_DISK_WARN_PCT = 80
_DISK_CRITICAL_PCT = 90
_RAM_WARN_PCT = 90


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

def short_gpu_name(name: str) -> str:
    """'NVIDIA GeForce RTX 5090' -> 'RTX 5090'. Vendor boilerplate says nothing on a card."""
    return re.sub(r"^(NVIDIA\s+)?(GeForce\s+)?", "", name or "").strip() or (name or "")


def lease_label(job_id: str) -> str:
    """A gate's lease is named after the service it guards: 'gate-comfyui' -> 'ComfyUI'."""
    if job_id.startswith("gate-"):
        service = job_id[len("gate-"):]
        return _TENANT_LABELS.get(service, service)
    return job_id


def _exit_code(status: str) -> int | None:
    m = re.search(r"\((\d+)\)", status or "")
    return int(m.group(1)) if m else None


def classify_container(state: str | None, health: str | None, status: str | None) -> str:
    """One verdict per container: up, starting, unhealthy, done, failed or stopped.

    'done' is exit 0: a one-shot job that finished, which must never read as a failure. 137 and
    143 are SIGKILL and SIGTERM, i.e. someone or something stopped it (an eviction included),
    which is 'stopped', not a crash.
    """
    if state == "running":
        if health == "unhealthy":
            return "unhealthy"
        if health == "starting":
            return "starting"
        return "up"
    if state == "restarting":
        return "failed"
    if state == "exited":
        code = _exit_code(status or "")
        if code == 0:
            return "done"
        if code in (137, 143):
            return "stopped"
        return "failed"
    return "stopped"


def uptime_from_status(status: str | None) -> str | None:
    m = re.match(r"^Up (.+?)(?: \(.*\))?$", status or "")
    return m.group(1) if m else None


def actions_for(verdict: str) -> list[str]:
    if verdict in ("up", "starting", "unhealthy"):
        return ["restart", "stop"]
    return ["start"]


def _verdict(row: dict | None) -> str:
    if not row:
        return "stopped"
    return classify_container(row.get("state"), row.get("health"), row.get("status"))


# ---------------------------------------------------------------------------------------------
# GPUs and the chat engine
# ---------------------------------------------------------------------------------------------

def _resident_gpu(registry: dict) -> str | None:
    for entry in (registry or {}).values():
        if entry.get("service") == "llamacpp":
            return entry.get("gpu_uuid")
    return None


def gpu_cards(hardware_gpus: list[dict], registry: dict, status_gpu: dict | None) -> list[dict]:
    """One card per GPU, biggest first: who lives on it, and who has borrowed it right now.

    Only the card the resident chat model lives on is scheduled, so only that card can be
    borrowed; leases in /status `running` are the borrowers.
    """
    resident_uuid = _resident_gpu(registry)
    borrowers = [lease_label(j.get("id", "")) for j in (status_gpu or {}).get("running", [])]
    cards = []
    for gpu in hardware_gpus or []:
        uuid = gpu.get("uuid")
        tenants = [
            _TENANT_LABELS.get(e.get("service", ""), e.get("service", ""))
            for e in (registry or {}).values()
            if e.get("gpu_uuid") == uuid
        ]
        cards.append({
            "uuid": uuid,
            "name": short_gpu_name(gpu.get("name", "")),
            "vram_used_gb": gpu.get("vram_used_gb"),
            "vram_total_gb": gpu.get("vram_total_gb"),
            "util_pct": gpu.get("utilization_pct"),
            "temp_c": gpu.get("temp_c"),
            "tenants": tenants,
            "borrowed_by": borrowers if uuid and uuid == resident_uuid else [],
        })
    cards.sort(key=lambda c: -(c.get("vram_total_gb") or 0))
    return cards


def chat_engine(status_gpu: dict | None, containers_by_id: dict) -> dict:
    """Which engine is answering chat right now: gpu, cpu (the fallback), none or unknown.

    A render borrowing the GPU evicts the resident model, and the gateway then serves chat from
    the CPU fallback: that is the system working, and the page must say so plainly rather than
    show the GPU server as down.
    """
    if status_gpu is None:
        return {"engine": "unknown", "reason": "The control plane did not answer"}
    evicted = "llamacpp" in (status_gpu.get("evicted_residents") or {})
    gpu_up = _verdict(containers_by_id.get("llamacpp")) in ("up", "starting") and not evicted
    if gpu_up:
        return {"engine": "gpu", "reason": ""}
    borrowers = [lease_label(j.get("id", "")) for j in status_gpu.get("running", [])]
    if borrowers:
        reason = f"The GPU is lent to {', '.join(borrowers)}"
    else:
        reason = "llama.cpp (GPU) is not running"
    if _verdict(containers_by_id.get("llamacpp-cpu")) in ("up", "starting"):
        return {"engine": "cpu", "reason": reason}
    return {"engine": "none", "reason": reason}


# ---------------------------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------------------------

def build_attention(cards: list[dict], containers_by_id: dict, hardware: dict,
                    status_gpu: dict | None) -> list[dict]:
    """What needs a person, most severe first. Finished jobs and stopped services are not
    problems; a failing health probe, an unhealthy or crashed container, a nearly full disk and
    a rejected GPU job are. A chat model evicted so a render can borrow its GPU is stopped on
    purpose, so its failing probe is not a problem either."""
    items: list[dict] = []
    evicted = set((status_gpu or {}).get("evicted_residents") or {})
    for card in cards or []:
        if card.get("ok") is False and (card.get("ops_service") or card.get("id")) not in evicted:
            items.append({"severity": "critical", "title": f"{card.get('name')} is not responding",
                          "detail": card.get("error") or "", "service": card.get("id")})
    for sid, row in sorted((containers_by_id or {}).items()):
        verdict = _verdict(row)
        if verdict == "unhealthy":
            items.append({"severity": "critical", "title": f"{sid} is unhealthy",
                          "detail": row.get("status", ""), "service": sid})
        elif verdict == "failed":
            items.append({"severity": "critical", "title": f"{sid} exited with an error",
                          "detail": row.get("status", ""), "service": sid})
    disk = (hardware or {}).get("disk_pct")
    if isinstance(disk, int | float) and disk >= _DISK_WARN_PCT:
        detail = ""
        if hardware.get("disk_used_gb") is not None and hardware.get("disk_total_gb") is not None:
            detail = f"{hardware['disk_used_gb']:,.1f} of {hardware['disk_total_gb']:,.1f} GB"
        items.append({"severity": "critical" if disk >= _DISK_CRITICAL_PCT else "warning",
                      "title": f"Disk {round(disk)}% full", "detail": detail, "service": None})
    ram = (hardware or {}).get("ram_pct")
    if isinstance(ram, int | float) and ram >= _RAM_WARN_PCT:
        items.append({"severity": "warning", "title": f"Memory {round(ram)}% used", "detail": "",
                      "service": None})
    for job in (status_gpu or {}).get("rejected", []) or []:
        items.append({"severity": "warning", "title": f"GPU job {job.get('id')} was rejected",
                      "detail": job.get("reason", ""), "service": None})
    items.sort(key=lambda i: 0 if i["severity"] == "critical" else 1)
    return items


# ---------------------------------------------------------------------------------------------
# renders, media, activity
# ---------------------------------------------------------------------------------------------

def _media_kind(filename: str) -> str:
    return _MEDIA_BY_SUFFIX.get(PurePosixPath(filename or "").suffix.lower(), "file")


def summarise_renders(history: dict, limit: int) -> list[dict]:
    """ComfyUI /history as a newest-first list: when, whether it worked, what it made."""
    renders = []
    for prompt_id, item in (history or {}).items():
        status = item.get("status") or {}
        stamps = [m[1].get("timestamp") for m in status.get("messages", [])
                  if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict) and m[1].get("timestamp")]
        outputs = []
        for node in (item.get("outputs") or {}).values():
            for files in node.values():
                if not isinstance(files, list):
                    continue
                for f in files:
                    if isinstance(f, dict) and f.get("filename"):
                        outputs.append({"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                        "type": f.get("type", "output"), "media": _media_kind(f["filename"])})
        renders.append({
            "prompt_id": prompt_id,
            "ok": status.get("status_str") == "success",
            "ts": max(stamps) / 1000.0 if stamps else None,
            "kind": outputs[0]["media"] if outputs else "unknown",
            "outputs": outputs,
        })
    renders.sort(key=lambda r: r["ts"] or 0, reverse=True)
    return renders[:limit]


def summarise_media(queue: dict, history: dict, limit: int) -> dict:
    running = (queue or {}).get("queue_running") or []
    pending = (queue or {}).get("queue_pending") or []
    return {
        "running": {"prompt_id": running[0][1]} if running else None,
        "pending": len(pending),
        "recent": summarise_renders(history, limit),
    }


def merge_activity(leases: list[dict], audit: list[dict], renders: list[dict], limit: int) -> list[dict]:
    """Renders, GPU leases and operator actions in one newest-first feed.

    A gate's lease and the render it admitted are the same event, so gate leases are left out
    and the render stands for both. Read-only audit entries are dropped.
    """
    items: list[dict] = []
    for r in renders or []:
        if r.get("ts") is None:
            continue
        title = f"Render finished · {r['kind']}" if r.get("ok") else "Render failed"
        items.append({"ts": r["ts"], "kind": "render", "title": title,
                      "severity": "ok" if r.get("ok") else "critical"})
    for lease in leases or []:
        job_id = lease.get("id", "")
        if job_id.startswith("gate-"):
            continue
        ts = lease.get("ended") or lease.get("started")
        if ts is None:
            continue
        items.append({"ts": float(ts), "kind": "gpu",
                      "title": f"GPU lease {lease_label(job_id)} {lease.get('outcome', '')}".strip(),
                      "severity": "ok" if lease.get("outcome") == "completed" else "warning"})
    for entry in audit or []:
        action = entry.get("action", "")
        if action in _READ_ONLY_ACTIONS or action in _LEASE_ACTIONS or entry.get("ts") is None:
            continue
        target = entry.get("target") or ""
        title = _AUDIT_TITLES.get(action, "{action} {target}").format(action=action, target=target).strip()
        ok = entry.get("result") in ("ok", None)
        if not ok:
            title += _AUDIT_OUTCOME_SUFFIX.get(entry.get("result"), " (failed)")
        elif entry.get("dry_run"):
            title += " (dry run)"
        items.append({"ts": float(entry["ts"]), "kind": "action", "title": title,
                      "severity": "info" if ok else "warning"})
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------------------------

def served_file(v1_models: dict | None) -> str | None:
    """The file a llama-server actually loaded, from its /v1/models (id is the model path)."""
    data = (v1_models or {}).get("data") or []
    if not data or not data[0].get("id"):
        return None
    return PurePosixPath(str(data[0]["id"])).name


def resident_gpu_file(registry: dict) -> str | None:
    """The file the GPU chat server is declared to serve, whether or not it is loaded now."""
    for entry in (registry or {}).values():
        if entry.get("service") == "llamacpp":
            return (entry.get("source") or {}).get("file")
    return None


def in_use_files(served: dict, model_config: dict) -> set[str]:
    """Every model file a server depends on: what each server has loaded now, plus every file the
    current render loads (ops-controller /model-config `model_files`: the chat model and its
    projector, the CPU fallback's model, the embedding model). The render matters because a
    stopped, evicted or restarting server reports nothing loaded, and loads the render's file
    when it comes back."""
    used = {f for f in (served or {}).values() if f}
    for entry in (model_config or {}).get("model_files") or []:
        if entry.get("file"):
            used.add(entry["file"])
    if (model_config or {}).get("active_file"):
        used.add(model_config["active_file"])
    return used


def model_slots(model_config: dict, served: dict, disk_files: list[dict], throughput: dict) -> dict:
    stats = (throughput or {}).get("models") or {}

    def p50(file: str | None):
        return (stats.get(file) or {}).get("p50") if file else None

    on_disk = {f.get("name") for f in disk_files or []}
    used = in_use_files(served, model_config)
    active_id = (model_config or {}).get("active_model")
    gpu_file = served.get("gpu") or (model_config or {}).get("active_file")
    return {
        "gpu": {"catalog_id": active_id, "file": gpu_file, "loaded": served.get("gpu") is not None,
                "ctx": (model_config or {}).get("ctx_size"), "p50": p50(gpu_file)},
        "cpu": {"file": served.get("cpu"), "p50": p50(served.get("cpu"))},
        "embed": {"file": served.get("embed")},
        "catalog": [
            {"id": m.get("id"), "tier": m.get("tier"), "vram_gb": m.get("vram_gb"),
             "file": m.get("file"), "installed": m.get("file") in on_disk,
             "active": m.get("id") == active_id}
            for m in (model_config or {}).get("available", [])
        ],
        "files": [{"name": f.get("name"), "size": f.get("size"), "in_use": f.get("name") in used}
                  for f in disk_files or []],
    }


# ---------------------------------------------------------------------------------------------
# services table
# ---------------------------------------------------------------------------------------------

_GROUP_ORDER = ["Apps", "Inference", "Data", "Platform", "Tools (MCP)", "Network", "Jobs"]
_INFERENCE_CATEGORIES = frozenset({"inference", "voice"})
_NETWORK_SERVICES = frozenset({"caddy", "oauth2-proxy"})


def _group_for_card(card: dict) -> str:
    if not card.get("background"):
        return "Apps"
    if card.get("category") in _INFERENCE_CATEGORIES:
        return "Inference"
    return "Data"


def _group_for_container(sid: str, verdict: str) -> str:
    if sid.startswith("mcp-"):
        return "Tools (MCP)"
    if sid.startswith("tailnet-") or sid in _NETWORK_SERVICES:
        return "Network"
    if verdict == "done":
        return "Jobs"
    return "Platform"


def _row(compose: str | None, name: str, group: str, container: dict | None, open_url=None,
         error=None, card_id: str | None = None, probe_ok: bool | None = None,
         lent: bool = False) -> dict:
    if compose is None:
        # A probe-only card: a link and a health check, not one container, so its state is the
        # probe's and it offers no lifecycle controls.
        verdict = {True: "up", False: "unhealthy"}.get(probe_ok, "unknown")
        controllable = False
    else:
        verdict = _verdict(container)
        if verdict == "up" and error:
            verdict = "unhealthy"  # the container runs but its HTTP probe fails
        controllable = compose not in NOT_CONTROLLABLE
    # A resident evicted so a render can borrow its GPU must not be started from here: that
    # would put a second tenant on the leased card. It comes back when the lease is released.
    actions = actions_for(verdict) if controllable and not lent else []
    return {
        "compose": compose, "card_id": card_id, "name": name, "group": group, "verdict": verdict,
        "uptime": uptime_from_status((container or {}).get("status")),
        "status": (container or {}).get("status"), "open_url": open_url, "error": error,
        "controllable": controllable, "lent": lent, "actions": actions,
    }


def _card_container(card: dict, containers_by_id: dict) -> str | None:
    """The compose service a card controls: its declared ops_service; else a container with the
    card's own id, which is unambiguous; else none, and the card is probe-only."""
    if card.get("ops_service"):
        return card["ops_service"]
    if card.get("id") in containers_by_id:
        return card["id"]
    return None


def build_service_table(cards: list[dict], containers_by_id: dict,
                        status_gpu: dict | None = None) -> list[dict]:
    """Every container, grouped; catalog services keep their friendly names and Open links."""
    evicted = set((status_gpu or {}).get("evicted_residents") or {})
    rows: list[dict] = []
    claimed: set[str] = set()
    for card in cards or []:
        compose = _card_container(card, containers_by_id or {})
        if compose:
            claimed.add(compose)
        rows.append(_row(compose, card.get("name") or card.get("id"), _group_for_card(card),
                         (containers_by_id or {}).get(compose) if compose else None, card.get("open_url"),
                         card.get("error") if card.get("ok") is False else None,
                         card_id=card.get("id"), probe_ok=card.get("ok"), lent=compose in evicted))
    for sid, container in sorted((containers_by_id or {}).items()):
        if sid in claimed:
            continue
        rows.append(_row(sid, sid, _group_for_container(sid, _verdict(container)), container,
                         lent=sid in evicted))
    grouped = []
    for group in _GROUP_ORDER:
        members = [r for r in rows if r["group"] == group]
        if members:
            grouped.append({"group": group, "services": members})
    return grouped
