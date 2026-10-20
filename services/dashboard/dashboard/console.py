"""Pure builders for the dashboard's Overview, Services and Models pages.

Every function here takes plain dicts in the shapes the live sources return (control-plane
/status, /services and /registry/models; the dashboard's own hardware stats and throughput
store; ComfyUI's /history; llama-server's /v1/models) and returns what a page
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
    # The GPU chat service, whichever engine serves it (llama.cpp or NInfer).
    "inference-engine-gpu": "GPU chat",
    "inference-engine-embed": "Embeddings",
    "comfyui": "ComfyUI",
    "stt": "Whisper",
    "tts": "Kokoro",
}

# Audit actions that only read state. They are most of the log (Hermes polls) and none of them
# is something a person would want in an activity feed.
_READ_ONLY_ACTIONS = frozenset({
    "containers.list", "container.logs", "diagnostics.dstate", "logs", "project.logs",
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

# A median of fewer than this many samples is provisional: the page shows the count and marks
# it, never a bare number (one slow turn must not read as the model's speed).
PROVISIONAL_SAMPLE_THRESHOLD = 10


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
        if entry.get("service") == "inference-engine-gpu":
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
    evicted = "inference-engine-gpu" in (status_gpu.get("evicted_residents") or {})
    gpu_up = _verdict(containers_by_id.get("inference-engine-gpu")) in ("up", "starting") and not evicted
    if gpu_up:
        return {"engine": "gpu", "reason": ""}
    borrowers = [lease_label(j.get("id", "")) for j in status_gpu.get("running", [])]
    if borrowers:
        reason = f"The GPU is lent to {', '.join(borrowers)}"
    else:
        reason = "The GPU chat model is not running"
    if _verdict(containers_by_id.get("inference-engine-cpu")) in ("up", "starting"):
        return {"engine": "cpu", "reason": reason}
    return {"engine": "none", "reason": reason}


def chat_medians(throughput: dict, served: dict, registry: dict) -> dict:
    """The Overview's GPU and CPU median speeds, from the ROLE series: a model swap changes the
    file name, not the role, so the median survives it instead of restarting from zero samples.
    Each carries its sample count and span, and the file the server loads now (the sparkline's
    label), and says whether it is provisional (fewer than PROVISIONAL_SAMPLE_THRESHOLD samples),
    so the page never decides that for itself."""
    roles = (throughput or {}).get("roles") or {}
    resident = resident_gpu_file(registry)
    out = {}
    for key, role in (("gpu", "gpu-chat"), ("cpu", "cpu-fallback")):
        r = roles.get(role) or {}
        n = r.get("n") or 0
        out[key] = {
            "p50": r.get("p50"),
            "n": n,
            "provisional": 0 < n < PROVISIONAL_SAMPLE_THRESHOLD,
            "first_ts": r.get("first_ts"),
            "last_ts": r.get("last_ts"),
            "model": served.get(key) or (resident if key == "gpu" else None),
        }
    return out


# ---------------------------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------------------------

def build_attention(cards: list[dict], containers_by_id: dict, hardware: dict,
                    status_gpu: dict | None, host_rows: list[dict] | None = None) -> list[dict]:
    """What needs a person, most severe first. Finished jobs and stopped services are not
    problems; a failing health probe, an unhealthy or crashed container, a nearly full disk and
    a rejected GPU job are. A chat model evicted so a render can borrow its GPU is stopped on
    purpose, so its failing probe is not a problem either. `host_rows` are the Services table's
    rows outside the ordo project: a crashed one is critical and an unhealthy one a warning,
    each naming its project; stopping or finishing one is a choice, not a problem."""
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
    for row in sorted(host_rows or [], key=lambda r: str(r.get("name") or "").lower()):
        verdict = row.get("verdict")
        if verdict not in ("failed", "unhealthy"):
            continue
        detail = f"{row.get('project') or 'no project'}: {row.get('status') or ''}"
        if verdict == "failed":
            items.append({"severity": "critical", "title": f"{row.get('name')} exited with an error",
                          "detail": detail, "service": row.get("name")})
        else:
            items.append({"severity": "warning", "title": f"{row.get('name')} is unhealthy",
                          "detail": detail, "service": row.get("name")})
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


def drift(doctor: dict | None) -> dict:
    """ops-controller's GET /doctor as the Overview's Drift callout: the checks `ordo doctor` would
    flag. `available` is False when the control plane did not answer, so an unanswered check never
    reads as "no drift"."""
    if not isinstance(doctor, dict):
        return {"available": False, "findings": []}
    findings = [{"check": str(check.get("check") or ""), "detail": str(check.get("detail") or "")}
                for check in doctor.get("checks") or [] if isinstance(check, dict) and check.get("ok") is False]
    return {"available": True, "findings": findings}


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


# The GPU chat service, by its compose id (#415 renamed it from `llamacpp`): the resident a lease
# evicts when its VRAM demand cannot share the card.
_CHAT_SERVICE = "inference-engine-gpu"


def merge_activity(leases: list[dict], audit: list[dict], renders: list[dict], limit: int) -> list[dict]:
    """Renders, GPU leases and operator actions in one newest-first feed.

    Every item carries the same keys; the lease fields (holder, started, ended, duration_s,
    evicted_chat) are null/false on non-lease items. Gate leases are kept: the render they
    admitted stands beside them as its own item, and the lease says who held the GPU, for how
    long, and whether the chat model was out of the GPU while it ran (the broker records the
    evicted residents on the lease as it starts; an older record without them reads false).
    Read-only audit entries are dropped.
    """
    items: list[dict] = []
    for r in renders or []:
        if r.get("ts") is None:
            continue
        title = f"Render finished · {r['kind']}" if r.get("ok") else "Render failed"
        items.append({"ts": r["ts"], "kind": "render", "title": title,
                      "severity": "ok" if r.get("ok") else "critical",
                      "holder": None, "started": None, "ended": None, "duration_s": None,
                      "evicted_chat": False})
    for lease in leases or []:
        job_id = lease.get("id", "")
        ts = lease.get("ended") or lease.get("started")
        if ts is None:
            continue
        holder = lease_label(job_id)
        started, ended = lease.get("started"), lease.get("ended")
        duration = round(ended - started) if isinstance(started, int | float) \
            and isinstance(ended, int | float) and ended >= started else None
        outcome = lease.get("outcome", "")
        if job_id.startswith("gate-"):
            title = f"GPU lent to {holder}" + (" (rejected)" if outcome == "rejected" else "")
        else:
            title = f"GPU lease {holder} {outcome}".strip()
        items.append({"ts": float(ts), "kind": "gpu", "title": title,
                      "severity": "ok" if outcome == "completed" else "warning",
                      "holder": holder, "started": started, "ended": ended, "duration_s": duration,
                      "evicted_chat": _CHAT_SERVICE in (lease.get("evicted") or [])})
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
                      "severity": "info" if ok else "warning",
                      "holder": None, "started": None, "ended": None, "duration_s": None,
                      "evicted_chat": False})
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------------------------

# The model files the GPU chat service can load: llama.cpp GGUFs and NInfer artifacts. One list, so the
# disk scan, the gateway pin alias and the delete guard agree.
CHAT_MODEL_SUFFIXES = (".gguf", ".ninfer")


def served_file(v1_models: dict | None) -> str | None:
    """The file the GPU chat server actually loaded, from its /v1/models: llama.cpp's id is the model
    path, NInfer's is the artifact file (its --model-id, run-ninfer-serve.sh)."""
    data = (v1_models or {}).get("data") or []
    if not data or not data[0].get("id"):
        return None
    return PurePosixPath(str(data[0]["id"])).name


def resident_gpu_file(registry: dict) -> str | None:
    """The file the GPU chat server is declared to serve, whether or not it is loaded now."""
    for entry in (registry or {}).values():
        if entry.get("service") == "inference-engine-gpu":
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

    def slot_speed(file: str | None) -> dict:
        """A slot's median speed with its evidence: the count and span the page needs to mark a
        thin median provisional."""
        s = stats.get(file) or {} if file else {}
        n = s.get("sample_count") or 0
        return {"p50": s.get("p50"), "n": n, "provisional": 0 < n < PROVISIONAL_SAMPLE_THRESHOLD,
                "first_ts": s.get("first_ts"), "last_ts": s.get("last_ts")}

    on_disk = {f.get("name") for f in disk_files or []}
    used = in_use_files(served, model_config)
    active_id = (model_config or {}).get("active_model")
    gpu_file = served.get("gpu") or (model_config or {}).get("active_file")
    return {
        "gpu": {"catalog_id": active_id, "file": gpu_file, "loaded": served.get("gpu") is not None,
                "ctx": (model_config or {}).get("ctx_size"), **slot_speed(gpu_file)},
        "cpu": {"file": served.get("cpu"), **slot_speed(served.get("cpu"))},
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

# ordo/render/config.py ORDO_PROJECT: the compose project the stack runs as (a test holds them equal).
ORDO_PROJECT = "ordo"
# The verdicts a managed-project restart is offered for: ops-controller restarts a container that
# runs; starting a stopped one is a lifecycle verb its managed-project routes do not have yet.
_RESTARTABLE = frozenset({"up", "starting", "unhealthy"})


def _table_row(*, row_id: str, name: str, project: str | None, container: str | None, compose: str | None,
               card_id: str | None, verdict: str, status: str | None, open_url: str | None, error: str | None,
               lent: bool, actions: list[str], control: str, reason: str | None = None) -> dict:
    """One Services row. `control` says which routes act on it: "ordo" (the lifecycle routes),
    "managed" (a managed project's logs and restart), "host" (only the host can cycle it), "link"
    (a probe-only card) or "none" (read-only, `reason` says why)."""
    return {
        "id": row_id, "name": name, "project": project, "container": container, "compose": compose,
        "card_id": card_id, "verdict": verdict, "uptime": uptime_from_status(status), "status": status,
        "open_url": open_url, "error": error, "lent": lent, "actions": actions, "control": control,
        "reason": reason,
    }


def _ordo_row(compose: str | None, name: str, container: dict | None, open_url=None, error=None,
              card_id: str | None = None, probe_ok: bool | None = None, lent: bool = False,
              control_plane_up: bool = True) -> dict:
    if compose is None:
        # A probe-only card: a link and a health check, not one container, so its state is the
        # probe's and it offers no lifecycle controls.
        verdict = {True: "up", False: "unhealthy"}.get(probe_ok, "unknown")
        control = "link"
    elif not control_plane_up:
        # No container list to read: its state is unknown, not stopped, and a Start offered on a
        # guess could put a second copy of a running service on the stack.
        verdict = "unknown"
        control = "host" if compose in NOT_CONTROLLABLE else "ordo"
    else:
        verdict = _verdict(container)
        if verdict == "up" and error:
            verdict = "unhealthy"  # the container runs but its HTTP probe fails
        control = "host" if compose in NOT_CONTROLLABLE else "ordo"
    # A resident evicted so a render can borrow its GPU must not be started from here: that
    # would put a second tenant on the leased card. It comes back when the lease is released.
    actions = actions_for(verdict) if control == "ordo" and not lent and verdict != "unknown" else []
    return _table_row(
        row_id=f"ordo:{compose}" if compose else f"link:{card_id}", name=name, project=ORDO_PROJECT,
        container=(container or {}).get("name"), compose=compose, card_id=card_id, verdict=verdict,
        status=(container or {}).get("status"), open_url=open_url, error=error, lent=lent, actions=actions,
        control=control,
    )


def _foreign_row(project: str | None, name: str, host_row: dict | None, managed_row: dict | None,
                 managed_projects: set[str] | None) -> dict:
    """A container outside the ordo project, named by its container name (unique on the host). A
    managed project's own row (state plus docker's Status text) wins over the host-wide list's,
    whose `status` is docker's State."""
    if managed_row:
        state, health, status = managed_row.get("state"), managed_row.get("health"), managed_row.get("status")
    else:
        state, health, status = host_row.get("status"), host_row.get("health"), host_row.get("status_text")
    verdict = classify_container(state, health, status)
    actions: list[str] = []
    reason = None
    if not project:
        control, reason = "none", "not started by compose, so no managed project can include it"
    elif managed_projects is None:
        control, reason = "none", "the control plane did not say which projects it manages"
    elif project in managed_projects:
        control = "managed"
        actions = ["restart"] if verdict in _RESTARTABLE else []
    else:
        control, reason = "none", f"add {project} to managed_projects in ordo.yaml"
    return _table_row(
        row_id=f"host:{name}", name=name, project=project, container=name, compose=None,
        card_id=None, verdict=verdict, status=status, open_url=None, error=None, lent=False,
        actions=actions, control=control, reason=reason,
    )


def _card_container(card: dict, containers_by_id: dict) -> str | None:
    """The compose service a card controls: its declared ops_service; else the container it declares
    it runs in (`runs_in`); else a container with the card's own id, which is unambiguous; else none,
    and the card is probe-only."""
    if card.get("ops_service"):
        return card["ops_service"]
    if card.get("runs_in"):
        return card["runs_in"]
    if card.get("id") in containers_by_id:
        return card["id"]
    return None


def _sort_key(row: dict) -> tuple:
    """Ordo first, then the other projects alphabetically, then containers compose did not start."""
    project = row["project"]
    rank = 0 if project == ORDO_PROJECT else 1 if project else 2
    return rank, project or "", row["name"].lower(), row["id"]


def build_service_table(cards: list[dict], containers_by_id: dict,
                        status_gpu: dict | None = None, control_plane_up: bool = True,
                        host_containers: list[dict] | None = None, managed: dict | None = None) -> list[dict]:
    """Every container on the host as one flat, sorted list. Ordo's rows come from its catalog cards
    (friendly names, Open links, probe-only cards) and ops-controller's /services; every other
    container from the host-wide /containers, enriched by the managed projects' /projects. With the
    control plane down, a row bound to a container is `unknown` and offers no actions."""
    evicted = set((status_gpu or {}).get("evicted_residents") or {})
    containers_by_id = containers_by_id or {}
    rows: list[dict] = []
    claimed: set[str] = set()
    for card in cards or []:
        compose = _card_container(card, containers_by_id)
        if compose:
            claimed.add(compose)
        rows.append(_ordo_row(compose, card.get("name") or card.get("id"),
                              containers_by_id.get(compose) if compose else None, card.get("open_url"),
                              card.get("error") if card.get("ok") is False else None,
                              card_id=card.get("id"), probe_ok=card.get("ok"), lent=compose in evicted,
                              control_plane_up=control_plane_up))
    for sid, container in sorted(containers_by_id.items()):
        if sid not in claimed:
            rows.append(_ordo_row(sid, sid, container, lent=sid in evicted))

    ordo_names = {c.get("name") for c in containers_by_id.values() if c.get("name")}
    managed_projects = None if managed is None else {p.get("project") for p in managed.get("projects") or []}
    foreign: dict[str, dict] = {}  # container name -> its project and its host-wide and managed rows
    for row in host_containers or []:
        name, project = row.get("name"), row.get("project") or None
        if name and project != ORDO_PROJECT and name not in ordo_names:
            foreign[name] = {"project": project, "host": row, "managed": None}
    for entry in (managed or {}).get("projects") or []:
        for row in entry.get("containers") or []:
            if row.get("name"):
                seen = foreign.setdefault(row["name"], {"project": entry.get("project"), "host": None})
                seen["managed"] = row
    for name, seen in foreign.items():
        rows.append(_foreign_row(seen["project"], name, seen["host"], seen["managed"], managed_projects))
    rows.sort(key=_sort_key)
    return rows


def service_counts(rows: list[dict]) -> dict:
    """The Overview's container counts from the Services table's rows: every container on the
    host, in total and per project. A finished one-shot job ('done') is not a service that
    should be up, so it leaves the totals; a probe-only card is not a container at all, and with
    the control plane down an ordo row carries no container either, so the counts keep only what
    was read."""
    by_project: dict[str, dict] = {}
    up = 0
    total = 0
    for row in rows or []:
        if not row.get("container") or row["verdict"] == "done":
            continue
        total += 1
        stats = by_project.setdefault(row.get("project") or "", {"total": 0, "up": 0, "failed": 0,
                                                                 "unhealthy": 0})
        stats["total"] += 1
        if row["verdict"] == "up":
            up += 1
            stats["up"] += 1
        elif row["verdict"] in ("failed", "unhealthy"):
            stats[row["verdict"]] += 1
    return {"up": up, "total": total, "by_project": by_project}
