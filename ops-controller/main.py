"""Ops Controller — secure Docker Compose control plane."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import docker
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# ``audit`` lives next to this module. In production (uvicorn main:app) and
# in pytest with ``ops-controller/conftest.py`` it imports as a top-level
# module; from tests in ``tests/`` that load this file via
# ``spec_from_file_location`` without touching sys.path, fall back to loading
# the sibling file directly.
try:
    from audit import AuditLog
except ModuleNotFoundError:  # pragma: no cover — exercised via legacy tests
    import importlib.util as _ilu
    _audit_spec = _ilu.spec_from_file_location(
        "audit", str(Path(__file__).resolve().parent / "audit.py"),
    )
    _audit_mod = _ilu.module_from_spec(_audit_spec)
    _audit_spec.loader.exec_module(_audit_mod)
    AuditLog = _audit_mod.AuditLog

try:
    import model_registry
except ModuleNotFoundError:  # pragma: no cover
    import importlib.util as _ilu
    _mr_spec = _ilu.spec_from_file_location(
        "model_registry", str(Path(__file__).resolve().parent / "model_registry.py"),
    )
    model_registry = _ilu.module_from_spec(_mr_spec)
    _mr_spec.loader.exec_module(model_registry)

try:
    import llamacpp_flags as lf
except ModuleNotFoundError:  # pragma: no cover
    import importlib.util as _ilu
    _lf_spec = _ilu.spec_from_file_location(
        "llamacpp_flags", str(Path(__file__).resolve().parent / "llamacpp_flags.py"),
    )
    lf = _ilu.module_from_spec(_lf_spec)
    _lf_spec.loader.exec_module(lf)

app = FastAPI(title="Ops Controller", version="1.0.0")
logger = logging.getLogger(__name__)

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "ordo-ai-stack")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "")
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.log"))
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", "10485760"))  # 10MB default

# Services we allow operations on (allowlist)
ALLOWED_SERVICES = {
    "llamacpp", "llamacpp-embed", "dashboard", "open-webui", "model-gateway", "mcp-gateway",
    "comfyui", "n8n", "qdrant", "stt", "tts",
}

# .env keys we allow updating via the API
ENV_ALLOWED_KEYS = {
    "DEFAULT_MODEL",
    "OPEN_WEBUI_DEFAULT_MODEL",
    "LLAMACPP_MODEL",
    "LLAMACPP_CTX_SIZE",
    "LLAMACPP_EMBED_MODEL",
    "LLAMACPP_MMPROJ",
    "LLAMACPP_FLASH_ATTN",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION",
    "LLAMACPP_KV_CACHE_TYPE_K",
    "LLAMACPP_KV_CACHE_TYPE_V",
    "LLAMACPP_EXTRA_ARGS",
}

BASE_PATH = os.environ.get("BASE_PATH", ".")
COMPOSE_FILE_ENV = os.environ.get("COMPOSE_FILE", "docker-compose.yml")
# On-disk GGUF directory (chat models + mmproj) shown in the model-config UI.
MODELS_DIR = Path(os.environ.get("LLAMACPP_MODELS_DIR", "/workspace/models/gguf"))
# Services that template LLAMACPP_CTX_SIZE and must also recreate when ctx changes.
MODEL_CONFIG_CTX_CONSUMERS = ["model-gateway"]

# Services whose GPU pin the dashboard may change.
GPU_ASSIGNABLE_SERVICES = {"llamacpp", "llamacpp-embed", "comfyui", "stt", "tts"}
GPU_ASSIGNMENTS_PATH = Path("/workspace/overrides/gpu-assignments.yml")

# Full UUID pattern: used by /gpu/assign and /registry/assign-gpu.
_GPU_UUID_RE = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

REGISTRY = model_registry.ModelRegistry(
    registry_path=Path(os.environ.get("MODEL_REGISTRY_PATH", "/data/model-registry.json")),
    env_path=Path(os.environ.get("OPS_ENV_PATH", "/workspace/.env")),
    gpu_assignments_path=GPU_ASSIGNMENTS_PATH,
)


def _reconcile_registry_on_startup() -> None:
    """Seed the model registry from .env + gpu-assignments.yml at startup.

    SEED-ONLY: records that already exist are left untouched. Safe to call
    multiple times (reconcile is idempotent). Mirrors the guarded pattern
    used for the ComfyUI guardian thread start — any failure logs a warning
    and is swallowed so a missing / corrupted env never prevents the controller
    from starting.
    """
    try:
        REGISTRY.reconcile()
        logger.info("Model registry reconciled from .env + gpu-assignments on startup")
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup reconcile failed (non-fatal): %s", exc)


_reconcile_registry_on_startup()


def parse_gpu_assignments_yaml(text: str) -> dict:
    """Parse the fixed-format overrides/gpu-assignments.yml into {service: uuid}.
    Delegates to model_registry.parse_gpu_assignments_yaml which accepts both
    single- and double-quoted UUIDs, so a write via /registry/assign-gpu (single
    quotes) followed by a /gpu/assign rollback parse both produce the same map."""
    return model_registry.parse_gpu_assignments_yaml(text)


def render_gpu_assignments(assignments: dict) -> str:
    """Render {service: uuid} to the override YAML.

    Delegates to model_registry.render_gpu_assignments_yaml so both the legacy
    /gpu/assign path and the /registry/assign-gpu path always emit the same
    quote style and header, preventing the cross-style parse mismatch that could
    wipe pins during a rollback."""
    return model_registry.render_gpu_assignments_yaml(assignments)


class GpuAssignBody(BaseModel):
    service: str
    gpu_uuid: str
    confirm: bool = False


def _write_gpu_assignments(mapping: dict) -> None:
    GPU_ASSIGNMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GPU_ASSIGNMENTS_PATH.with_suffix(".tmp")
    tmp.write_text(render_gpu_assignments(mapping), encoding="utf-8")
    os.replace(str(tmp), str(GPU_ASSIGNMENTS_PATH))


def apply_gpu_assignment(service: str, gpu_uuid: str) -> dict:
    """Read current assignments, set service->gpu_uuid, write atomically. Returns new map."""
    current = {}
    if GPU_ASSIGNMENTS_PATH.exists():
        current = parse_gpu_assignments_yaml(GPU_ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    current[service] = gpu_uuid
    _write_gpu_assignments(current)
    return current


# ---------------------------------------------------------------------------
# Shared helpers used by /gpu/assign AND /registry/* endpoints
# ---------------------------------------------------------------------------

def _recreate_service(service: str, request=None) -> dict:
    """Run docker-compose up -d --no-deps <service>. Raises HTTPException on failure."""
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["up", "-d", "--no-deps", service]
    env = {**os.environ, "BASE_PATH": BASE_PATH}
    operator_home = os.environ.get("OPERATOR_HOME")
    if operator_home:
        env["HOME"] = operator_home
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd="/workspace", env=env, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Service recreate timed out after 120 seconds")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout)[:500])
    return {"ok": True, "service": service, "action": "recreated"}


def _write_text_atomic(path: Path, text: str) -> None:
    """Write text to path atomically via a unique tempfile + os.replace.

    Uses tempfile.mkstemp so concurrent calls never collide on the same .tmp
    filename (which could truncate each other's in-flight writes).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _set_env_keys(kv: dict, request=None) -> None:
    """Write each key→value pair into the .env file (OPS_ENV_PATH).

    Only keys in ENV_ALLOWED_KEYS are permitted; raises HTTPException for others.
    Each key is upserted atomically (replace existing line or append).
    """
    env_path = REGISTRY.env_path
    for key, value in kv.items():
        if key not in ENV_ALLOWED_KEYS:
            raise HTTPException(status_code=400, detail=f"Key not in allowlist: {key!r}")
        if "\n" in str(value) or "\r" in str(value):
            raise HTTPException(status_code=400, detail=f"Illegal newline in env value for {key}")
    if not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        content = ""
    else:
        content = env_path.read_text(encoding="utf-8")
    for key, value in kv.items():
        pattern = rf"^{re.escape(key)}=.*"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + f"\n{key}={value}\n"
    _write_text_atomic(env_path, content)


# ---------------------------------------------------------------------------
# Model-config control plane (dashboard) — registry overrides -> .env -> recreate
# ---------------------------------------------------------------------------

def _active_chat_record():
    """The enabled single-model llamacpp (chat) registry record, or None."""
    for rec in REGISTRY.list_models().values():
        if rec.service == "llamacpp" and rec.runtime == "single-model" and rec.enabled:
            return rec
    return None


def _read_env_values(keys):
    """Current values for `keys` from the active (uncommented) .env lines."""
    env_path = REGISTRY.env_path
    out = {}
    if not env_path.exists():
        return out
    content = env_path.read_text(encoding="utf-8")
    for key in keys:
        m = re.search(rf"^{re.escape(key)}=(.*)$", content, re.MULTILINE)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[key] = v
    return out


def _render_model_config_to_env(effective):
    """Upsert every managed flag into .env in place. The ^KEY= anchor updates only
    the active line, so commented presets in the MODEL CONFIGS block survive."""
    env_path = REGISTRY.env_path
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key in sorted(lf.ENV_KEYS):
        if key not in effective:
            continue
        val = str(effective[key])
        if "\n" in val or "\r" in val:
            raise HTTPException(status_code=400, detail=f"Illegal newline in {key}")
        pattern = rf"^{re.escape(key)}=.*"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={val}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + f"\n{key}={val}\n"
    _write_text_atomic(env_path, content)


def _list_ggufs(mmproj=False):
    """On-disk GGUF basenames. mmproj=True -> only mmproj-* files; else chat models."""
    try:
        names = sorted(p.name for p in MODELS_DIR.glob("*.gguf"))
    except OSError:
        return []
    if mmproj:
        return [n for n in names if n.startswith("mmproj")]
    return [n for n in names if not n.startswith("mmproj") and "embed" not in n.lower()]


def _live_gpus() -> dict:
    """Return {uuid: {"name", "total_gb", "used_gb", "util"}} via nvidia-smi.

    Returns {} if nvidia-smi is unavailable or returns no output.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=gpu_uuid,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        out: dict = {}
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            uuid, name, total_mib, used_mib, util = parts[:5]
            try:
                out[uuid] = {
                    "name": name,
                    "total_gb": round(float(total_mib) / 1024.0, 1),
                    "used_gb": round(float(used_mib) / 1024.0, 1),
                    "util": int(float(util)),
                }
            except (ValueError, TypeError):
                continue
        return out
    except Exception:
        return {}


# Model download (ComfyUI files)
COMFYUI_MODELS_DIR = Path(os.environ.get("COMFYUI_MODELS_DIR", "/models/comfyui"))
# Same layout as docker-compose: ${BASE_PATH}/data/comfyui-storage → comfyui /root
COMFYUI_CUSTOM_NODES_DIR = Path("/workspace/data/comfyui-storage/ComfyUI/custom_nodes")
COMFYUI_CONTAINER_NAME = os.environ.get("COMFYUI_CONTAINER_NAME", "comfyui")
COMFYUI_CATEGORIES = (
    "checkpoints", "loras", "text_encoders", "latent_upscale_models",
    "vae", "unet", "clip", "clip_vision", "controlnet", "embeddings",
    "upscale_models", "diffusion_models", "vae_approx",
)
_NODE_PATH_SEGMENTS = re.compile(r"^[a-zA-Z0-9._-]+$")
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip() or os.environ.get("HUGGING_FACE_HUB_TOKEN", "").strip()
_dl_lock = threading.Lock()
_dl_status: dict = {
    "running": False, "output": "", "done": True, "success": None,
    "progress": 0, "filename": "", "category": "",
}
_pull_lock = threading.Lock()
_pull_status: dict = {
    "running": False, "output": "", "done": True, "success": None,
    "pack": "",
}
_gguf_pull_lock = threading.Lock()
_gguf_pull_status: dict = {
    "running": False, "output": "", "done": True, "success": None,
    "repos": "",
}

# ComfyUI ↔ llamacpp VRAM serialization guardian.
# When enabled, a background thread polls ComfyUI's queue. Non-empty queue → stop
# the target service (llamacpp) to free VRAM for ComfyUI workflows. Queue drained
# for COMFYUI_DRAIN_SECONDS → start the target again. Prevents the OOM-spillover
# state where both services share the 32GB 5090 and decode collapses to <1 tok/s.
#
# Tradeoff: in-flight Hermes requests during a ComfyUI workflow will fail with
# APIConnectionError. Hermes session state is preserved in its database, so
# conversation history survives — only the one killed turn is lost.
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
COMFYUI_SERIALIZE_LLAMACPP = os.environ.get("COMFYUI_SERIALIZE_LLAMACPP", "0").strip().lower() in ("1", "true", "yes", "on")
COMFYUI_QUEUE_POLL_SECONDS = float(os.environ.get("COMFYUI_QUEUE_POLL_SECONDS", "2"))
COMFYUI_DRAIN_SECONDS = float(os.environ.get("COMFYUI_DRAIN_SECONDS", "20"))
COMFYUI_GUARDIAN_TARGET = os.environ.get("COMFYUI_GUARDIAN_TARGET", "llamacpp")

# Phase 1: after the guardian's drain elapses and we resume the target service,
# also POST to ComfyUI's /free endpoint so PyTorch's caching allocator releases
# back to the OS. Without this, ComfyUI keeps the post-job VRAM pool warm and
# llamacpp comes back into a near-OOM card. Default ON; harmless no-op when
# ComfyUI is already idle (200 OK either way).
COMFYUI_FREE_AFTER_DRAIN = os.environ.get("COMFYUI_FREE_AFTER_DRAIN", "1").strip().lower() in ("1", "true", "yes", "on")

# Phase 2: VRAM-pressure watchdog. Independent of ComfyUI's queue state.
# Polls total GPU memory use; if it exceeds OPS_VRAM_PRESSURE_GB, POST to
# ComfyUI /free to release whatever PyTorch is holding. Re-checks until used
# memory falls below OPS_VRAM_RECOVERY_GB (or pressure_gb - 4 if recovery is
# unset). Disabled when OPS_VRAM_PRESSURE_GB <= 0 (default).
OPS_VRAM_PRESSURE_GB = float(os.environ.get("OPS_VRAM_PRESSURE_GB", "0"))
OPS_VRAM_RECOVERY_GB = float(os.environ.get("OPS_VRAM_RECOVERY_GB", "0"))
OPS_VRAM_POLL_SECONDS = float(os.environ.get("OPS_VRAM_POLL_SECONDS", "30"))

# ── Self-heal watchdog ────────────────────────────────────────────────────────
# Opt-in background task that restarts exited compose services after a grace
# window. Prevents the "operator stopped for rebuild and died" scenario AND
# the "cascade orphaned model-gateway/mcp-gateway and nobody noticed" one.
# Disabled by default — set OPS_HERMES_WATCHDOG_ENABLED=1 to enable. The env
# var name keeps the OPS_HERMES_ prefix for backward compatibility with the
# original Hermes-only watchdog; the watched set is no longer Hermes-only.
OPS_HERMES_WATCHDOG_ENABLED = os.environ.get("OPS_HERMES_WATCHDOG_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
OPS_HERMES_WATCHDOG_INTERVAL_SECONDS = float(os.environ.get("OPS_HERMES_WATCHDOG_INTERVAL_SECONDS", "30"))
OPS_HERMES_WATCHDOG_GRACE_SECONDS = float(os.environ.get("OPS_HERMES_WATCHDOG_GRACE_SECONDS", "60"))
OPS_HERMES_WATCHDOG_PAUSE_FILE = os.environ.get("OPS_HERMES_WATCHDOG_PAUSE_FILE", "/data/watchdog.paused")

# Comma-separated list of compose service NAMES the watchdog must NOT touch.
# Defaults to ops-controller (we cannot watch our own host) plus any one-shot
# init container service names if you have them. Override via env if needed.
OPS_WATCHDOG_EXCLUDE = {
    s.strip() for s in os.environ.get(
        "OPS_WATCHDOG_EXCLUDE",
        "ops-controller,comfyui-manager-setup,comfyui-mcp-image,orchestration-mcp-image",
    ).split(",") if s.strip()
}

_WATCHDOG_TASK: asyncio.Task | None = None

_guardian_lock = threading.Lock()
_guardian_status: dict = {
    "enabled": COMFYUI_SERIALIZE_LLAMACPP,
    "state": "disabled",  # disabled | idle | paused | draining | error
    "target": COMFYUI_GUARDIAN_TARGET,
    "comfyui_url": COMFYUI_URL,
    "poll_seconds": COMFYUI_QUEUE_POLL_SECONDS,
    "drain_seconds": COMFYUI_DRAIN_SECONDS,
    "comfyui_queue": {"running": 0, "pending": 0, "reachable": False},
    "last_transition": None,
    "last_error": "",
    "paused_by_us": False,
}


_cached_docker: docker.DockerClient | None = None

# Structured audit log for the Hermes-facing privileged endpoints
# (containers.list / container.logs / container.restart / compose.{up,down,restart}).
# Schema: ``{ts, caller, action, target, result, ...extra}``. One JSON line per call.
_audit_log = AuditLog(os.environ.get("AUDIT_LOG_PATH", "/data/audit.jsonl"))


def _docker_client() -> docker.DockerClient:
    global _cached_docker  # noqa: PLW0603
    if _cached_docker is not None:
        try:
            _cached_docker.ping()
            return _cached_docker
        except Exception:
            logger.warning("Docker client stale — reconnecting")
            _cached_docker = None
    _cached_docker = docker.from_env()
    return _cached_docker


async def verify_token(request: Request) -> None:
    """Verify Bearer token. Use as Depends(verify_token)."""
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="Ops controller authentication not configured. Set OPS_CONTROLLER_TOKEN in your .env file and restart.")
    src = request.client.host if request.client else "unknown"
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        logger.warning("AUTH_FAIL reason=missing_bearer path=%s src=%s", request.url.path, src)
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth[7:].strip()
    if not hmac.compare_digest(token, OPS_CONTROLLER_TOKEN):
        logger.warning("AUTH_FAIL reason=invalid_token path=%s src=%s", request.url.path, src)
        raise HTTPException(status_code=403, detail="Invalid token")


def _maybe_rotate_audit_log() -> None:
    """If audit log exceeds AUDIT_LOG_MAX_BYTES, rotate: .log -> .log.1, start fresh."""
    try:
        if not AUDIT_LOG_PATH.exists():
            return
        if AUDIT_LOG_PATH.stat().st_size < AUDIT_LOG_MAX_BYTES:
            return
        rotated = AUDIT_LOG_PATH.with_suffix(AUDIT_LOG_PATH.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        AUDIT_LOG_PATH.rename(rotated)
    except Exception as e:
        logger.warning("Audit log rotation failed: %s", e)


def _audit(
    action: str,
    resource: str = "",
    result: str = "ok",
    detail: str = "",
    correlation_id: str = "",
    metadata: dict | None = None,
):
    """Append to audit log. Schema: docs/audit/SCHEMA.md. Rotates by size when over limit."""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate_audit_log()
        entry = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "action": action,
            "resource": resource or "",
            "actor": "dashboard",
            "result": result,
            "detail": detail or "",
        }
        if correlation_id:
            entry["correlation_id"] = correlation_id
        if metadata:
            entry["metadata"] = metadata
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error("Audit write failed: %s", e)


def _get_containers():
    """Get all containers for compose project."""
    client = _docker_client()
    return client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"},
    )


def _containers_for_service(service_id: str):
    """Get containers for a compose service."""
    client = _docker_client()
    return client.containers.list(
        all=True,
        filters={
            "label": [
                f"com.docker.compose.project={COMPOSE_PROJECT}",
                f"com.docker.compose.service={service_id}",
            ]
        },
    )


def _cpu_pct_from_stats(stats: dict) -> float:
    """Compute CPU% from one docker stats sample using precpu_stats delta. Matches `docker stats` CLI math."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = int(cpu["cpu_usage"]["total_usage"]) - int(pre["cpu_usage"]["total_usage"])
        system_delta = int(cpu["system_cpu_usage"]) - int(pre.get("system_cpu_usage") or 0)
        online_cpus = int(cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1)
        if system_delta <= 0 or cpu_delta < 0:
            return 0.0
        return round((cpu_delta / system_delta) * online_cpus * 100.0, 1)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _mem_from_stats(stats: dict) -> tuple[float, float]:
    """Return (mem_gb, mem_pct). Subtracts inactive_file (cgroup v2) or cache (v1) like `docker stats`."""
    try:
        ms = stats["memory_stats"]
        usage = int(ms.get("usage") or 0)
        inner = ms.get("stats") or {}
        sub = int(inner.get("inactive_file") or inner.get("cache") or 0)
        used = max(0, usage - sub)
        limit = int(ms.get("limit") or 0)
        if limit <= 0:
            return (round(used / 1e9, 2), 0.0)
        return (round(used / 1e9, 2), round(used / limit * 100.0, 1))
    except (KeyError, TypeError, ValueError):
        return (0.0, 0.0)


def _container_host_pids(container) -> list[int]:
    """Host-visible PIDs for a running container via `docker top`. Returns [] on any failure."""
    try:
        info = container.top(ps_args="-eo pid,comm")
    except Exception:
        return []
    procs = (info or {}).get("Processes") or []
    pids: list[int] = []
    for row in procs:
        if not row:
            continue
        raw = str(row[0]).strip()
        if raw.isdigit():
            pids.append(int(raw))
    return pids


def _nvml_vraam_by_pid() -> tuple[dict[int, int], dict]:
    """Return ({pid: vram_bytes}, gpu_summary). pid_map empty when per-PID VRAM is unavailable (e.g. WSL2/WDDM)."""
    default_gpu = {"total_gb": 0.0, "used_gb": 0.0, "utilization_pct": 0, "per_pid_available": False}
    try:
        import pynvml
        pynvml.nvmlInit()
    except Exception as e:
        logger.debug("NVML init failed: %s", e)
        return {}, default_gpu
    try:
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mi = pynvml.nvmlDeviceGetMemoryInfo(h)
        ut = pynvml.nvmlDeviceGetUtilizationRates(h)
        total_b = int(mi.total)
        used_b = int(mi.used)
        pids: dict[int, int] = {}
        has_per_pid = False
        for getter in (pynvml.nvmlDeviceGetComputeRunningProcesses,
                       pynvml.nvmlDeviceGetGraphicsRunningProcesses):
            try:
                for p in getter(h):
                    mem = getattr(p, "usedGpuMemory", None) or getattr(p, "used_gpu_memory", None)
                    if mem is None:
                        continue
                    mem_b = int(mem)
                    if mem_b <= 0:
                        continue
                    has_per_pid = True
                    pids[int(p.pid)] = pids.get(int(p.pid), 0) + mem_b
            except pynvml.NVMLError:
                pass
        return pids, {
            "total_gb": round(total_b / 1e9, 1),
            "used_gb": round(used_b / 1e9, 1),
            "utilization_pct": int(ut.gpu),
            "per_pid_available": has_per_pid,
        }
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _read_total_vram_used_gb() -> float | None:
    """Total GPU memory in use, in GB. None when NVML unavailable."""
    _, gpu = _nvml_vraam_by_pid()
    if not gpu.get("total_gb"):
        return None
    return float(gpu.get("used_gb") or 0.0)


def _call_comfyui_free(reason: str = "") -> tuple[bool, str]:
    """POST to ComfyUI /free so PyTorch's caching allocator returns memory.

    Non-fatal — on any failure, return (False, detail). Caller logs and
    continues. The body shape matches ComfyUI's documented contract: unloading
    the model graph and emptying the cache. Other shapes ({}, just one of the
    two flags) are also accepted by ComfyUI but produce a partial release.
    """
    try:
        import urllib.request
        body = json.dumps({"unload_models": True, "free_memory": True}).encode()
        req = urllib.request.Request(
            f"{COMFYUI_URL}/free",
            method="POST",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            return (ok, f"http={resp.status} reason={reason}" if reason else f"http={resp.status}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:200]}")


@app.get("/health")
def health():
    """Controller health. No auth required. Verifies Docker daemon reachable.

    Defined as `def` (not `async def`) so FastAPI dispatches it on the
    threadpool. The body calls `_docker_client().ping()` synchronously, which
    blocks on Docker's socket with the SDK's 60s default timeout. Running that
    on the asyncio event loop froze every other request — manifesting as a
    full accept queue and silent timeouts even on loopback. Same change
    applied to every other handler that does sync Docker SDK I/O below.
    """
    try:
        _docker_client().ping()
    except Exception as e:
        logger.warning("Health check failed: %s", e)
        return JSONResponse(status_code=503, content={"ok": False, "error": "Docker daemon unavailable"})
    return {"ok": True}


@app.get("/services")
def list_services():
    """List compose services. No auth for read-only."""
    try:
        containers = _get_containers()
        seen = set()
        services = []
        for c in containers:
            labels = c.labels or {}
            svc = labels.get("com.docker.compose.service", c.name)
            if svc in seen:
                continue
            seen.add(svc)
            state = c.status if hasattr(c, "status") else "unknown"
            services.append({"id": svc, "name": svc, "state": state})
        return {"services": sorted(services, key=lambda s: s["id"])}
    except Exception as e:
        logger.warning("Service list failed: %s", e)
        return JSONResponse(status_code=503, content={"services": [], "detail": "Docker unavailable"})


# --- Hermes-facing privileged endpoints --------------------------------------
# Plan C narrows Hermes to call ops-controller over HTTP instead of holding
# /var/run/docker.sock directly. These verbs (containers.list / container.logs
# / container.restart / compose.{up,down,restart}) are the ones Hermes needs;
# every call emits one line to ``_audit_log``.

@app.get("/containers")
def list_containers(_: None = Depends(verify_token)):
    """List all containers visible to the docker daemon. Auth required, audited."""
    client = _docker_client()
    out = []
    for c in client.containers.list(all=True):
        image = ""
        try:
            tags = getattr(c.image, "tags", None) or []
            image = tags[0] if tags else (getattr(c.image, "id", "") or "")
        except Exception:
            image = ""
        out.append({
            "name": c.name,
            "status": c.status,
            "image": image,
        })
    _audit_log.record(action="containers.list", target="*", result="ok", caller="hermes")
    return out


@app.get("/containers/{name}/logs", response_class=PlainTextResponse)
def container_logs(
    name: str, tail: int = 100, since: str | None = None,
    _: None = Depends(verify_token),
):
    """Tail any container's logs by name. Auth required, audited."""
    client = _docker_client()
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        _audit_log.record(action="container.logs", target=name, result="not_found", caller="hermes")
        raise HTTPException(status_code=404, detail=f"container {name} not found")
    kwargs: dict = {"tail": tail, "timestamps": True}
    if since:
        kwargs["since"] = since
    raw = c.logs(**kwargs)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    _audit_log.record(
        action="container.logs", target=name, result="ok", caller="hermes", tail=tail,
    )
    return text


@app.post("/containers/{name}/restart")
def container_restart(name: str, _: None = Depends(verify_token)):
    """Restart any container by name. Auth required, audited."""
    client = _docker_client()
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        _audit_log.record(
            action="container.restart", target=name, result="not_found", caller="hermes",
        )
        raise HTTPException(status_code=404, detail=f"container {name} not found")
    c.restart()
    _audit_log.record(
        action="container.restart", target=name, result="ok", caller="hermes",
    )
    return {"name": name, "restarted": True}


# Whole-stack compose ops require ``confirm: true`` to prevent accidents
# (or prompt-injection-driven restarts of the entire stack). Service names
# are validated against a strict allowlist regex to prevent shell injection
# via the subprocess argv.

class ComposeOpRequest(BaseModel):
    service: str | None = Field(default=None, max_length=64)
    confirm: bool = False


_COMPOSE_SERVICE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _run_compose(verb: str, service: str | None) -> subprocess.CompletedProcess:
    # Use standalone docker-compose binary (installed in Dockerfile)
    # rather than docker CLI + compose plugin which is not available.
    cmd = ["docker-compose", verb]
    if verb == "up":
        cmd.append("-d")  # always detach, regardless of whether a service is named
    if service:
        cmd.append(service)

    # Compose interpolates ${HOME} when resolving secret bind sources
    # (docker-compose.yml top-level `secrets:` block uses
    # ${HOME}/.ai-toolkit/runtime/secrets/...). The ops-controller process
    # runs as `appuser` with HOME=/home/appuser, which does NOT match the
    # operator's host home where those secret files live. Override HOME in
    # the subprocess env to whatever OPERATOR_HOME points at — set on the
    # ops-controller service in docker-compose.yml as
    # `OPERATOR_HOME=${HOME}` so it inherits the operator's $HOME at the
    # moment they ran `docker compose up`.
    env = os.environ.copy()
    operator_home = os.environ.get("OPERATOR_HOME")
    if operator_home:
        env["HOME"] = operator_home

    return subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        cwd=os.environ.get("COMPOSE_PROJECT_DIR", "/workspace"),
    )


def _compose_endpoint(verb: str, body: ComposeOpRequest):
    # Validate service name explicitly (rather than via pydantic) so we
    # return a plain 400 instead of FastAPI's 422 ValidationError envelope.
    if body.service is not None and not _COMPOSE_SERVICE_NAME.fullmatch(body.service):
        raise HTTPException(status_code=400, detail="service name contains illegal characters")
    if body.service is None and not body.confirm:
        raise HTTPException(status_code=400, detail="whole-stack compose op requires confirm=true")
    target = body.service or "all"
    proc = _run_compose(verb, body.service)
    result = "ok" if proc.returncode == 0 else "fail"
    _audit_log.record(
        action=f"compose.{verb}", target=target, result=result, caller="hermes",
        rc=proc.returncode, stderr=proc.stderr[-500:] if proc.stderr else "",
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"compose {verb} failed: {proc.stderr[-200:]}")
    return {"verb": verb, "target": target, "stdout": proc.stdout[-2000:]}


def _watchdog_paused() -> bool:
    return Path(OPS_HERMES_WATCHDOG_PAUSE_FILE).exists()


def _watchdog_decision(container, now: datetime, grace_seconds: float) -> tuple[str, str]:
    """Pure decision for one container. Returns (decision, detail).

    decisions: skip-running, skip-no-finish, skip-bad-finish, skip-grace, act.
    """
    if container.status != "exited":
        return ("skip-running", f"status={container.status}")
    finished_at_str = container.attrs.get("State", {}).get("FinishedAt", "") or ""
    if not finished_at_str or finished_at_str == "0001-01-01T00:00:00Z":
        return ("skip-no-finish", finished_at_str or "<empty>")
    try:
        finished_at = datetime.fromisoformat(finished_at_str.replace("Z", "+00:00"))
    except ValueError:
        return ("skip-bad-finish", finished_at_str)
    age = (now - finished_at).total_seconds()
    if age < grace_seconds:
        return ("skip-grace", f"age={age:.0f}s grace={grace_seconds:.0f}s")
    return ("act", f"age={age:.0f}s")


def _watchdog_iteration() -> None:
    """One synchronous watchdog cycle. Safe to call from tests."""
    if _watchdog_paused():
        _audit_log.record(
            action="watchdog.paused", target="", result="ok", caller="watchdog",
            pause_file=OPS_HERMES_WATCHDOG_PAUSE_FILE,
        )
        return

    try:
        client = _docker_client()
        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"},
        )
    except Exception:
        logger.exception("[watchdog] docker query failed")
        return

    # Auto-discover the watched set: every compose service in this project
    # whose restart policy is unless-stopped (covers all long-running services)
    # and that we're allowed to touch (not on the EXCLUDE list — ops-controller
    # itself, init/one-shot containers, anything the operator explicitly opted
    # out). This way new services added to docker-compose.yml are watched
    # automatically; no code change required.
    now = datetime.now(UTC)
    by_service: dict[str, list] = {}
    for c in containers:
        svc = c.labels.get("com.docker.compose.service")
        if not svc or svc in OPS_WATCHDOG_EXCLUDE:
            continue
        try:
            policy = (c.attrs.get("HostConfig", {}) or {}).get("RestartPolicy", {}).get("Name", "")
        except Exception:
            policy = ""
        if policy != "unless-stopped":
            continue
        by_service.setdefault(svc, []).append(c)

    for svc, targets in by_service.items():
        for c in targets:
            decision, detail = _watchdog_decision(c, now, OPS_HERMES_WATCHDOG_GRACE_SECONDS)

            # Healthy state — debug only. Auditing every iteration would flood the log.
            if decision == "skip-running":
                logger.debug("[watchdog] %s skip-running (%s)", svc, detail)
                continue

            if decision in ("skip-no-finish", "skip-bad-finish"):
                logger.warning("[watchdog] %s %s detail=%s", svc, decision, detail)
                _audit_log.record(
                    action=f"watchdog.{decision}", target=svc, result="ok",
                    caller="watchdog", detail=detail,
                )
                continue

            if decision == "skip-grace":
                _audit_log.record(
                    action="watchdog.skipped-grace", target=svc, result="ok",
                    caller="watchdog", detail=detail,
                )
                continue

            # decision == "act"
            # Use container.start() via the SDK rather than `docker-compose up`.
            # The watchdog's job is to revive a container the operator stopped,
            # not to recreate from spec — and avoiding compose sidesteps the
            # ${HOME}-relative secret-path resolution mess (compose secrets
            # interpolate $HOME against the *calling* process, so running it
            # from inside ops-controller (HOME=/home/appuser) hits a path that
            # doesn't exist on the docker host).
            logger.info("[watchdog] starting %s (%s)", svc, detail)
            try:
                c.start()
                _audit_log.record(
                    action="watchdog.acted", target=svc, result="ok",
                    caller="watchdog", detail=detail, container_id=c.short_id,
                )
            except Exception as e:
                _audit_log.record(
                    action="watchdog.acted", target=svc, result="fail",
                    caller="watchdog", detail=detail,
                    container_id=c.short_id, error=str(e)[:200],
                )


async def _hermes_watchdog_loop() -> None:
    """Asyncio loop: run one iteration, sleep, repeat. Cancelled on shutdown.

    `_watchdog_iteration` makes blocking Docker SDK calls (containers.list,
    container.start) — running it on the event loop would freeze every HTTP
    handler for the duration of those calls. On Docker Desktop Windows the
    daemon can take seconds-to-minutes to respond, and the 60s default SDK
    timeout had been stalling uvicorn long enough that healthchecks passed
    (listener still bound) but no request body was ever read. Run on a worker
    thread so the event loop stays responsive.
    """
    while True:
        try:
            await asyncio.to_thread(_watchdog_iteration)
        except asyncio.CancelledError:
            logger.info("[watchdog] cancelled")
            raise
        except Exception:
            logger.exception("[watchdog] iteration crashed")
        await asyncio.sleep(OPS_HERMES_WATCHDOG_INTERVAL_SECONDS)


@app.post("/compose/up")
async def compose_up(body: ComposeOpRequest, _: None = Depends(verify_token)):
    return _compose_endpoint("up", body)


@app.post("/compose/down")
async def compose_down(body: ComposeOpRequest, _: None = Depends(verify_token)):
    return _compose_endpoint("down", body)


@app.post("/compose/restart")
async def compose_restart(body: ComposeOpRequest, _: None = Depends(verify_token)):
    return _compose_endpoint("restart", body)


class ConfirmBody(BaseModel):
    confirm: bool = False
    dry_run: bool = False


def _correlation_id(request: Request) -> str:
    """Extract X-Request-ID for audit correlation. Sanitized to prevent log injection."""
    raw = (request.headers.get("X-Request-ID") or "").strip()
    return re.sub(r"[^a-zA-Z0-9_\-.]", "", raw)[:128]


@app.post("/services/{service_id}/start")
async def service_start(
    service_id: str, body: ConfirmBody, request: Request,
    _: None = Depends(verify_token),
):
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {service_id} not in allowlist")
    if body.dry_run:
        return {"would": "start", "service": service_id}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    containers = _containers_for_service(service_id)
    if not containers:
        raise HTTPException(status_code=404, detail=f"No container found for service {service_id}")
    errs = []
    for c in containers:
        try:
            c.start()
        except Exception as e:
            errs.append(str(e))
    _audit(
        "start", service_id, "error" if errs else "ok", "; ".join(errs) if errs else "",
        correlation_id=_correlation_id(request),
    )
    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    return {"ok": True, "service": service_id, "action": "started"}


@app.post("/services/{service_id}/stop")
async def service_stop(
    service_id: str, body: ConfirmBody, request: Request,
    _: None = Depends(verify_token),
):
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {service_id} not in allowlist")
    if body.dry_run:
        return {"would": "stop", "service": service_id}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    containers = _containers_for_service(service_id)
    if not containers:
        raise HTTPException(status_code=404, detail=f"No container found for service {service_id}")
    errs = []
    for c in containers:
        try:
            c.stop(timeout=30)
        except Exception as e:
            errs.append(str(e))
    _audit(
        "stop", service_id, "error" if errs else "ok", "; ".join(errs) if errs else "",
        correlation_id=_correlation_id(request),
    )
    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    return {"ok": True, "service": service_id, "action": "stopped"}


# Restart debounce: collapse rapid repeat restart requests for the SAME service
# (e.g. an agent retry loop) into a single in-flight restart. Without this, a
# few-seconds-apart hammer issues N overlapping `docker restart` calls and
# thrashes the service. Tunable via OPS_RESTART_DEBOUNCE_SECONDS (0 disables).
RESTART_DEBOUNCE_SECONDS = float(os.environ.get("OPS_RESTART_DEBOUNCE_SECONDS", "20"))
_restart_lock = threading.Lock()
_restart_state: dict[str, dict] = {}  # service_id -> {"inflight": bool, "ts": float}


@app.post("/services/{service_id}/restart")
async def service_restart(
    service_id: str, body: ConfirmBody, request: Request,
    _: None = Depends(verify_token),
):
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {service_id} not in allowlist")
    if body.dry_run:
        return {"would": "restart", "service": service_id}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    # Debounce rapid repeats so a retry-loop "storm" collapses into one restart.
    now = time.monotonic()
    with _restart_lock:
        st = _restart_state.get(service_id) or {}
        if st.get("inflight"):
            _audit("restart", service_id, "debounced", "restart already in progress",
                   correlation_id=_correlation_id(request))
            return {"ok": True, "service": service_id, "action": "debounced",
                    "detail": "restart already in progress"}
        last_ts = st.get("ts", 0.0)
        if RESTART_DEBOUNCE_SECONDS > 0 and last_ts and (now - last_ts) < RESTART_DEBOUNCE_SECONDS:
            ago = now - last_ts
            _audit("restart", service_id, "debounced", f"restarted {ago:.0f}s ago",
                   correlation_id=_correlation_id(request))
            return {"ok": True, "service": service_id, "action": "debounced",
                    "detail": f"restarted {ago:.0f}s ago; within {RESTART_DEBOUNCE_SECONDS:.0f}s debounce window"}
        _restart_state[service_id] = {"inflight": True, "ts": last_ts}
    errs = []
    try:
        containers = _containers_for_service(service_id)
        if not containers:
            raise HTTPException(status_code=404, detail=f"No container found for service {service_id}")
        for c in containers:
            try:
                c.restart(timeout=30)
            except Exception as e:
                errs.append(str(e))
    finally:
        with _restart_lock:
            _restart_state[service_id] = {"inflight": False, "ts": time.monotonic()}
    _audit(
        "restart", service_id, "error" if errs else "ok", "; ".join(errs) if errs else "",
        correlation_id=_correlation_id(request),
    )
    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    return {"ok": True, "service": service_id, "action": "restarted"}


@app.get("/services/{service_id}/logs")
async def service_logs(
    service_id: str, request: Request, tail: int = 100,
    _: None = Depends(verify_token),
):
    """Tail service logs. Auth required."""
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {service_id} not in allowlist")
    containers = _containers_for_service(service_id)
    if not containers:
        raise HTTPException(status_code=404, detail=f"No container found for service {service_id}")
    tail_n = max(1, min(tail, 500))
    lines = []
    for c in containers:
        try:
            out = c.logs(tail=tail_n, timestamps=True).decode("utf-8", errors="replace")
            lines.append(f"=== {c.name} ===\n{out}")
        except Exception as e:
            lines.append(f"=== {c.name} ===\nError: {e}")
    _audit(
        "logs", service_id, "ok", "",
        correlation_id=_correlation_id(request),
        metadata={"tail": tail_n},
    )
    return {"logs": "\n".join(lines), "service": service_id}


class PullBody(BaseModel):
    services: list[str] = []


@app.post("/images/pull")
async def images_pull(body: PullBody, request: Request, _: None = Depends(verify_token)):
    svcs = [s for s in body.services if s in ALLOWED_SERVICES]
    if not svcs:
        raise HTTPException(status_code=400, detail="No allowed services specified")
    errs = []
    for svc in svcs:
        containers = _containers_for_service(svc)
        for c in containers:
            try:
                c.image.pull()
            except Exception as e:
                errs.append(f"{svc}: {e}")
    _audit(
        "pull", ",".join(svcs), "error" if errs else "ok", "; ".join(errs) if errs else "",
        correlation_id=_correlation_id(request),
    )
    if errs:
        raise HTTPException(status_code=500, detail="; ".join(errs))
    return {"ok": True, "services": svcs}


@app.get("/gpu/assignments")
async def gpu_assignments(_: None = Depends(verify_token)):
    """Return current service->GPU-uuid pins from the override file."""
    if not GPU_ASSIGNMENTS_PATH.exists():
        return {"assignments": {}}
    return {"assignments": parse_gpu_assignments_yaml(GPU_ASSIGNMENTS_PATH.read_text(encoding="utf-8"))}


@app.post("/gpu/assign")
async def gpu_assign(body: GpuAssignBody, request: Request, _: None = Depends(verify_token)):
    """Pin a GPU service to a specific GPU UUID, then recreate it so the pin takes effect."""
    if body.service not in GPU_ASSIGNABLE_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {body.service} not GPU-assignable")
    if not _GPU_UUID_RE.fullmatch(body.gpu_uuid):
        raise HTTPException(status_code=400, detail="Invalid GPU UUID format")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    prev = {}
    if GPU_ASSIGNMENTS_PATH.exists():
        prev = parse_gpu_assignments_yaml(GPU_ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    apply_gpu_assignment(body.service, body.gpu_uuid)
    try:
        _recreate_service(body.service, request)
    except HTTPException as exc:
        _write_gpu_assignments(prev)  # rollback
        detail = str(exc.detail)
        _audit("gpu_assign", body.service, "error", detail[:200], correlation_id=_correlation_id(request))
        raise
    _audit("gpu_assign", body.service, "ok", body.gpu_uuid, correlation_id=_correlation_id(request))

    # Keep the model registry in sync so it never goes stale when an old client
    # uses the legacy /gpu/assign path. Update any record whose service matches.
    # Guarded so a registry failure never breaks the legacy response.
    try:
        for mid, record in REGISTRY.list_models().items():
            if record.service == body.service:
                record.gpu_uuid = body.gpu_uuid
                REGISTRY.upsert(record)
    except Exception as _reg_exc:  # noqa: BLE001
        logger.warning("legacy /gpu/assign: registry sync failed (non-fatal): %s", _reg_exc)

    return {"ok": True, "service": body.service, "gpu_uuid": body.gpu_uuid, "action": "reassigned"}


@app.get("/mcp/containers")
def mcp_containers(_: None = Depends(verify_token)):
    """List MCP server containers (spawned by mcp-gateway). Auth required."""
    try:
        client = _docker_client()
        all_containers = client.containers.list(all=True)
        mcp_containers = []
        for c in all_containers:
            image = (c.image.tags[0] if c.image.tags else str(c.image)) if hasattr(c, "image") else ""
            # MCP gateway spawns containers with mcp/* images
            if "mcp/" in image or (hasattr(c, "name") and "mcp" in (c.name or "").lower()):
                server_id = image.split("/")[-1].split(":")[0] if "/" in image else (c.name or "unknown")
                mcp_containers.append({
                    "id": server_id,
                    "name": c.name,
                    "status": c.status if hasattr(c, "status") else "unknown",
                    "image": image,
                })
        return {"containers": mcp_containers}
    except Exception as e:
        return {"containers": [], "error": str(e)}


class EnvSetBody(BaseModel):
    key: str
    value: str
    confirm: bool = False


@app.post("/env/set")
async def env_set(body: EnvSetBody, request: Request, _: None = Depends(verify_token)):
    """Update a single allowed key in .env. Requires confirm: true. Audited."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    if body.key not in ENV_ALLOWED_KEYS:
        raise HTTPException(status_code=400, detail=f"Key not in allowlist: {body.key!r}")
    if "\n" in body.value or "\r" in body.value:
        raise HTTPException(status_code=400, detail="Value must not contain newlines")
    # Prevent shell injection via LLAMACPP_EXTRA_ARGS (value is word-split in run script)
    if body.key == "LLAMACPP_EXTRA_ARGS":
        if not re.fullmatch(r"[a-zA-Z0-9 _.=:/-]*", body.value):
            raise HTTPException(status_code=400, detail="LLAMACPP_EXTRA_ARGS: only alphanumeric, spaces, dashes, dots, equals, colons, slashes allowed")
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=".env not found at /workspace/.env")
    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(body.key)}=.*"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{body.key}={body.value}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{body.key}={body.value}\n"
    tmp_path = env_path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(env_path))
    _audit("env_set", body.key, "ok", f"len={len(body.value)}", correlation_id=_correlation_id(request))
    return {"ok": True, "key": body.key}


@app.get("/env/{key}")
async def env_get(key: str, _: None = Depends(verify_token)):
    """Read a single allowed key from /workspace/.env (same file env_set writes)."""
    if key not in ENV_ALLOWED_KEYS:
        raise HTTPException(status_code=400, detail=f"Key not in allowlist: {key!r}")
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        return {"key": key, "value": ""}
    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}=(.*)$"
    m = re.search(pattern, content, re.MULTILINE)
    raw = m.group(1).rstrip() if m else ""
    # Strip optional surrounding quotes (common in .env examples)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return {"key": key, "value": raw}


class ModelConfigBody(BaseModel):
    overrides: dict = Field(default_factory=dict)
    confirm: bool = False
    dry_run: bool = False


@app.get("/model-config")
async def model_config_get(_: None = Depends(verify_token)):
    """Full model-control state for the dashboard: flag descriptors, defaults,
    active model, the active model's overrides, effective values, current .env,
    and on-disk model/mmproj lists."""
    # Current state = the DEPLOYED .env (filled with defaults), so the UI always
    # reflects what's actually running — not a possibly-stale registry record.
    base = lf.defaults()
    running = _read_env_values(lf.ENV_KEYS)
    effective = dict(base)
    effective.update(running)
    rec = _active_chat_record()
    if not effective.get("LLAMACPP_MODEL") and rec and rec.source.get("file"):
        effective["LLAMACPP_MODEL"] = rec.source["file"]
    # An "override" = an effective value that differs from the baseline default.
    overrides = {k: effective[k] for k in lf.ENV_KEYS
                 if k in effective and effective[k] != base.get(k, "")}
    return {
        "flags": lf.descriptors(),
        "defaults": base,
        "active_model": effective.get("LLAMACPP_MODEL", ""),
        "overrides": overrides,
        "effective": lf.flag_view(effective),
        "running": running,
        "models": _list_ggufs(),
        "mmprojs": _list_ggufs(mmproj=True),
    }


@app.post("/model-config")
async def model_config_post(body: ModelConfigBody, request: Request,
                            _: None = Depends(verify_token)):
    """Validate + apply model-config overrides via the ONE write path: persist to
    the registry, render into .env, recreate llamacpp (+ ctx consumers)."""
    errs = lf.validate_all({k: v for k, v in body.overrides.items() if v is not None})
    if errs:
        raise HTTPException(status_code=400, detail={"validation": errs})
    if body.dry_run:
        return {"would": "apply", "overrides": body.overrides}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set {\"confirm\": true} to apply.")

    rec = _active_chat_record()
    if rec is None:
        raise HTTPException(status_code=404, detail="No active single-model llamacpp record")

    config = dict(rec.config)
    source_file = rec.source.get("file", "")
    ctx_touched = False
    for k, v in body.overrides.items():
        if k == "LLAMACPP_MODEL":
            if v:
                source_file = str(v)
            config.pop("LLAMACPP_MODEL", None)
            continue
        if k == "LLAMACPP_CTX_SIZE":
            ctx_touched = True
        if v is None:
            config.pop(k, None)
        else:
            config[k] = str(v)

    overrides = dict(config)
    if source_file:
        overrides["LLAMACPP_MODEL"] = source_file
    effective = lf.compute_effective(lf.defaults(), overrides)

    model = effective.get("LLAMACPP_MODEL", "")
    if not model:
        raise HTTPException(status_code=400, detail="A model file must be set")
    if not (MODELS_DIR / model).exists():
        raise HTTPException(status_code=400, detail=f"Model file not found: {model}")

    _render_model_config_to_env(effective)
    rec.config = config
    rec.source = {**rec.source, "file": source_file}
    rec.updated_by = "model-config"
    REGISTRY.upsert(rec)

    services = ["llamacpp"] + (MODEL_CONFIG_CTX_CONSUMERS if ctx_touched else [])
    for svc in services:
        _recreate_service(svc, request)

    _audit("model_config", model, "ok", f"keys={sorted(body.overrides)}",
           correlation_id=_correlation_id(request))
    return {"ok": True, "active_model": model,
            "effective": lf.flag_view(effective), "recreated": services}


@app.post("/services/{service_id}/recreate")
async def service_recreate(
    service_id: str, body: ConfirmBody, request: Request,
    _: None = Depends(verify_token),
):
    """Recreate a service container via docker compose up -d so new env vars take effect."""
    if service_id not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service {service_id} not in allowlist")
    if body.dry_run:
        return {"would": "recreate", "service": service_id}
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["up", "-d", "--no-deps", service_id]
    env = {**os.environ, "BASE_PATH": BASE_PATH}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd="/workspace", env=env, timeout=120)
    except subprocess.TimeoutExpired:
        _audit("recreate", service_id, "error", "timed out after 120s",
               correlation_id=_correlation_id(request))
        raise HTTPException(status_code=504, detail="Service recreate timed out after 120 seconds")
    ok = result.returncode == 0
    detail = (result.stderr or result.stdout)[:200] if not ok else ""
    _audit("recreate", service_id, "ok" if ok else "error", detail,
           correlation_id=_correlation_id(request))
    if not ok:
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout)[:500])
    return {"ok": True, "service": service_id, "action": "recreated"}


@app.get("/audit")
async def audit(limit: int = 50, _: None = Depends(verify_token)):
    """Read audit log. Auth required."""
    if not AUDIT_LOG_PATH.exists():
        return {"entries": []}
    from collections import deque
    with open(AUDIT_LOG_PATH, encoding="utf-8", errors="replace") as f:
        tail = deque(f, maxlen=limit)
    entries = []
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": entries}


def _validate_custom_node_path(node_path: str) -> str:
    """Relative path under ComfyUI custom_nodes; POSIX segments, no traversal."""
    s = node_path.strip().strip("/").replace("\\", "/")
    if not s or len(s) > 240:
        raise HTTPException(status_code=400, detail="Invalid node_path")
    if ".." in s:
        raise HTTPException(status_code=400, detail="Invalid node_path")
    for seg in s.split("/"):
        if not seg or not _NODE_PATH_SEGMENTS.match(seg):
            raise HTTPException(status_code=400, detail=f"Invalid path segment: {seg!r}")
    return s


def _comfyui_pip_install_sync(node_path: str) -> dict:
    """Run pip install -r inside the comfyui container. Called via asyncio.to_thread."""
    req_host = COMFYUI_CUSTOM_NODES_DIR / node_path / "requirements.txt"
    if not req_host.is_file():
        return {
            "ok": False,
            "http_status": 404,
            "detail": f"No requirements.txt at custom_nodes/{node_path}/requirements.txt",
        }
    req_container = f"/root/ComfyUI/custom_nodes/{node_path}/requirements.txt"
    try:
        client = _docker_client()
        container = client.containers.get(COMFYUI_CONTAINER_NAME)
    except docker.errors.NotFound:
        return {
            "ok": False,
            "http_status": 503,
            "detail": f"Container {COMFYUI_CONTAINER_NAME!r} not found — start comfyui first",
        }
    except Exception as e:
        return {"ok": False, "http_status": 503, "detail": f"Docker: {e}"}
    try:
        er = container.exec_run(
            ["python3", "-m", "pip", "install", "-r", req_container],
            demux=False,
        )
        exit_code = getattr(er, "exit_code", None)
        output = getattr(er, "output", b"")
        if exit_code is None and isinstance(er, tuple):
            exit_code, output = er[0], er[1]
    except Exception as e:
        return {"ok": False, "http_status": 500, "detail": f"exec failed: {e}"}
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output or "")
    if len(text) > 12000:
        text = text[:12000] + "\n… [truncated]"
    return {
        "ok": bool(exit_code == 0),
        "exit_code": int(exit_code) if exit_code is not None else -1,
        "output": text,
        "node_path": node_path,
    }


class InstallNodeRequirementsBody(BaseModel):
    node_path: str
    confirm: bool = False


@app.post("/comfyui/install-node-requirements")
async def comfyui_install_node_requirements(
    body: InstallNodeRequirementsBody,
    request: Request,
    _: None = Depends(verify_token),
):
    """Install Python deps for a custom node pack (pip -r) inside the running comfyui container."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    node_path = _validate_custom_node_path(body.node_path)
    result = await asyncio.to_thread(_comfyui_pip_install_sync, node_path)
    if result.get("http_status"):
        detail = result.get("detail", result)
        raise HTTPException(status_code=int(result["http_status"]), detail=detail)
    _audit(
        "comfyui_pip_install",
        node_path,
        "ok" if result.get("ok") else "error",
        (result.get("output") or "")[:300],
        correlation_id=_correlation_id(request),
        metadata={"exit_code": result.get("exit_code")},
    )
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result)
    return result


@app.get("/stats/services")
def stats_services(_: None = Depends(verify_token)):
    """Per-compose-service CPU/RAM/VRAM. Read-only, auth required (same as other ops routes).

    Sync def: the body iterates every running container and calls Docker's
    `c.stats(stream=False)`, which takes ~1s per container while the daemon
    samples cgroup counters. With ~17 services this single endpoint blocks
    for ~17 seconds. Running it as `async def` froze the entire uvicorn
    event loop — manifesting as a stuck accept queue and timed-out probes
    from every peer. Threadpool dispatch (this change) keeps the loop free.
    """
    try:
        containers = _get_containers()
    except Exception as e:
        logger.warning("stats/services: docker list failed: %s", e)
        return {"gpu": None, "services": {}, "vram_aggregate_unavailable": True}

    vram_by_pid, gpu = _nvml_vraam_by_pid()
    vram_aggregate_unavailable = not gpu["per_pid_available"]

    services: dict[str, dict] = {}
    for c in containers:
        svc = (c.labels or {}).get("com.docker.compose.service")
        if not svc:
            continue
        row = services.setdefault(svc, {
            "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
            "vram_gb": 0.0, "vram_pct": 0.0, "running": False,
        })
        status = getattr(c, "status", "") or ""
        if status != "running":
            continue
        row["running"] = True
        try:
            sample = c.stats(stream=False)
        except Exception as e:
            logger.debug("stats sample failed for %s: %s", svc, e)
            continue
        row["cpu_pct"] = _cpu_pct_from_stats(sample)
        row["mem_gb"], row["mem_pct"] = _mem_from_stats(sample)
        if vram_by_pid:
            pids = _container_host_pids(c)
            total_b = sum(vram_by_pid.get(pid, 0) for pid in pids)
            if total_b > 0 and gpu["total_gb"] > 0:
                row["vram_gb"] = round(total_b / 1e9, 2)
                row["vram_pct"] = round(total_b / (gpu["total_gb"] * 1e9) * 100.0, 1)

    gpu_out = None if gpu["total_gb"] == 0 else {k: v for k, v in gpu.items() if k != "per_pid_available"}
    return {
        "gpu": gpu_out,
        "services": services,
        "vram_aggregate_unavailable": vram_aggregate_unavailable,
    }


# --- Model downloads (ComfyUI files) ---


def _auto_detect_category(url: str, filename: str) -> str:
    """Guess ComfyUI model category from URL path or filename."""
    parts = url.lower()
    fn = filename.lower()
    combined = parts + " " + fn
    # Check exact category names first (longest match wins)
    for cat in sorted(COMFYUI_CATEGORIES, key=len, reverse=True):
        if cat in combined:
            return cat
    # Keyword fallbacks
    if "lora" in combined:
        return "loras"
    if "text_encoder" in combined or "clip" in combined:
        return "text_encoders"
    if "vae" in combined:
        return "vae"
    if "unet" in combined:
        return "unet"
    if "controlnet" in combined:
        return "controlnet"
    if "upscale" in combined:
        return "upscale_models"
    if "embedding" in combined:
        return "embeddings"
    return "checkpoints"


def _run_model_download(url: str, category: str, filename: str, correlation_id: str = "") -> None:
    """Resumable file download to COMFYUI_MODELS_DIR. Runs in a daemon thread."""
    with _dl_lock:
        _dl_status.update({
            "running": True, "output": f"Starting: {filename}", "done": False,
            "success": None, "progress": 0, "filename": filename, "category": category,
        })
    dest_dir = COMFYUI_MODELS_DIR / category
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _audit("model_download", f"{category}/{filename}", "error", str(e)[:200], correlation_id=correlation_id)
        with _dl_lock:
            _dl_status.update({"output": f"Cannot create dir: {e}", "success": False, "running": False, "done": True})
        return

    dest = dest_dir / filename
    temp_path = dest.with_suffix(dest.suffix + ".tmp")
    try:
        start_byte = temp_path.stat().st_size if temp_path.exists() else 0
        req_headers = {"User-Agent": "ordo-ai-stack/1.0"}
        if HF_TOKEN and ("huggingface.co" in url or "hf-mirror.com" in url):
            req_headers["Authorization"] = f"Bearer {HF_TOKEN}"
        if start_byte > 0:
            req_headers["Range"] = f"bytes={start_byte}-"
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            with client.stream("GET", url, headers=req_headers) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                if filename.endswith(".safetensors") and ct and "octet-stream" not in ct and "safetensors" not in ct:
                    body_preview = ""
                    try:
                        for chunk in r.iter_bytes(chunk_size=512):
                            body_preview = chunk.decode("utf-8", errors="replace").strip()[:200]
                            break
                    except Exception:
                        pass
                    hint = " (gated model? Agree to license at huggingface.co, ensure HF_TOKEN is valid)"
                    if body_preview:
                        hint = f" — response: {body_preview[:150]}..."
                    raise ValueError(f"Unexpected Content-Type {ct!r}; expected octet-stream{hint}")
                r.raise_for_status()
                total_header = r.headers.get("Content-Range") or r.headers.get("Content-Length")
                total = 0
                if total_header and "/" in str(total_header):
                    total = int(str(total_header).split("/")[-1].strip())
                elif r.headers.get("Content-Length"):
                    total = int(r.headers["Content-Length"]) + (start_byte or 0)
                total_mb = total / (1024 * 1024) if total else 0
                downloaded = start_byte
                with open(temp_path, "ab" if start_byte else "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        dl_mb = downloaded / (1024 * 1024)
                        pct = int(downloaded * 100 / total) if total else 0
                        msg = f"Downloading {filename} → {category}/\n"
                        msg += f"{dl_mb:.0f} / {total_mb:.0f} MB ({pct}%)" if total else f"{dl_mb:.0f} MB downloaded"
                        with _dl_lock:
                            _dl_status["output"] = msg
                            _dl_status["progress"] = pct
        temp_path.rename(dest)
        _audit("model_download", f"{category}/{filename}", "ok", url[:200], correlation_id=correlation_id)
        with _dl_lock:
            _dl_status["success"] = True
            _dl_status["output"] += f"\nDone — saved to {category}/{filename}"
    except Exception as e:
        logger.error("Model download failed: %s", e)
        _audit("model_download", f"{category}/{filename}", "error", str(e)[:200], correlation_id=correlation_id)
        with _dl_lock:
            _dl_status["output"] += f"\nError: {e}"
            _dl_status["success"] = False
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    finally:
        with _dl_lock:
            _dl_status["running"] = False
            _dl_status["done"] = True


_MODEL_DOWNLOAD_ALLOWED_HOSTS = {
    "huggingface.co", "hf-mirror.com", "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co", "cdn-lfs-eu-1.huggingface.co",
    "civitai.com", "github.com", "objects.githubusercontent.com",
}


def _validate_download_url(url: str) -> None:
    """Block SSRF: only allow HTTPS to known model-hosting domains, reject private IPs."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Cannot parse hostname from URL")
    if host not in _MODEL_DOWNLOAD_ALLOWED_HOSTS:
        raise ValueError(
            f"Host {host!r} not in allowed list. "
            f"Allowed: {', '.join(sorted(_MODEL_DOWNLOAD_ALLOWED_HOSTS))}"
        )
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local:
                raise ValueError(f"Host {host!r} resolves to private/reserved IP {addr}")
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host {host!r}: {exc}") from exc


class ModelDownloadRequest(BaseModel):
    url: str
    category: str = ""
    filename: str = ""


@app.post("/models/download")
async def models_download(body: ModelDownloadRequest, request: Request, _: None = Depends(verify_token)):
    """Start a resumable file download to the ComfyUI models directory. Auth required. Audited."""
    url = body.url.strip()
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL must start with https://")
    try:
        _validate_download_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    with _dl_lock:
        if _dl_status.get("running"):
            raise HTTPException(status_code=409, detail="A download is already in progress")
    filename = body.filename.strip() or url.split("/")[-1].split("?")[0]
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid or undetectable filename")
    category = body.category.strip()
    if category and category not in COMFYUI_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {COMFYUI_CATEGORIES}")
    if not category:
        category = _auto_detect_category(url, filename)
    thread = threading.Thread(
        target=_run_model_download,
        args=(url, category, filename, _correlation_id(request)),
        daemon=True,
    )
    thread.start()
    return {"status": "started", "category": category, "filename": filename}


@app.get("/models/download/status")
async def models_download_status(_: None = Depends(verify_token)):
    """Poll active download progress. Auth required."""
    with _dl_lock:
        return dict(_dl_status)


def _run_model_pull(packs_csv: str, correlation_id: str = "") -> None:
    """Run comfyui-model-puller via docker compose. COMFYUI_PACKS may be comma-separated (e.g. ltx-2.3-t2v-basic,ltx-2.3-extras)."""
    with _pull_lock:
        _pull_status.update(
            {
                "running": True,
                "output": f"Starting packs: {packs_csv}",
                "done": False,
                "success": None,
                "pack": packs_csv,
            }
        )
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["--profile", "comfyui-models", "run", "--rm", "-e", f"COMFYUI_PACKS={packs_csv}", "comfyui-model-puller"]
    env = {**os.environ, "BASE_PATH": BASE_PATH, "DATA_PATH": os.environ.get("DATA_PATH", BASE_PATH + "/data")}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd="/workspace",
            env=env,
        )
        output_lines = []
        for line in proc.stdout:
            output_lines.append(line.rstrip())
            with _pull_lock:
                _pull_status["output"] = "\n".join(output_lines[-20:])
        proc.wait(timeout=7200)
        ok = proc.returncode == 0
        _audit("model_pull", packs_csv, "ok" if ok else "error", f"exit={proc.returncode}", correlation_id=correlation_id)
        with _pull_lock:
            _pull_status["success"] = ok
            _pull_status["output"] = "\n".join(output_lines[-30:])
            if not ok:
                _pull_status["output"] += f"\nExit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("Model pull timed out after 7200s")
        _audit("model_pull", packs_csv, "error", "timeout after 7200s", correlation_id=correlation_id)
        with _pull_lock:
            _pull_status["success"] = False
            _pull_status["output"] += "\nError: timed out after 2 hours"
    except Exception as e:
        logger.error("Model pull failed: %s", e)
        _audit("model_pull", packs_csv, "error", str(e)[:200], correlation_id=correlation_id)
        with _pull_lock:
            _pull_status["success"] = False
            _pull_status["output"] += f"\nError: {e}"
    finally:
        with _pull_lock:
            _pull_status["running"] = False
            _pull_status["done"] = True


class ModelPullRequest(BaseModel):
    pack: str
    confirm: bool = False


def _valid_packs() -> set[str]:
    """Load valid pack names from models.json."""
    try:
        path = Path("/workspace/scripts/comfyui/models.json")
        if path.exists():
            data = json.loads(path.read_text())
            return set(data.get("packs", {}).keys())
    except Exception:
        pass
    return {"flux1-dev", "flux-schnell", "sd15", "sd35-medium", "sdxl"}


@app.get("/models/packs")
async def models_packs(_: None = Depends(verify_token)):
    """List ComfyUI model pack IDs and descriptions from scripts/comfyui/models.json."""
    path = Path("/workspace/scripts/comfyui/models.json")
    if not path.exists():
        raise HTTPException(status_code=404, detail="models.json not found in workspace")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid models.json: {e}") from e
    packs_out: dict = {}
    for pid, p in data.get("packs", {}).items():
        if not isinstance(p, dict):
            continue
        packs_out[pid] = {
            "description": p.get("description", ""),
            "model_count": len(p.get("models", [])),
        }
    return {"ok": True, "packs": packs_out}


@app.post("/models/pull")
async def models_pull(body: ModelPullRequest, request: Request, _: None = Depends(verify_token)):
    """Run comfyui-model-puller for one or more comma-separated packs (e.g. ltx-2.3-t2v-basic,ltx-2.3-extras). Auth required."""
    parts = [p.strip().lower() for p in (body.pack or "").split(",") if p.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="pack is required (comma-separated names allowed)")
    valid = _valid_packs()
    unknown = [p for p in parts if p not in valid]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pack(s): {unknown}. Valid: {', '.join(sorted(valid))}",
        )
    packs_csv = ",".join(parts)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    with _pull_lock:
        if _pull_status.get("running"):
            raise HTTPException(status_code=409, detail="A pull is already in progress")
    thread = threading.Thread(
        target=_run_model_pull,
        args=(packs_csv, _correlation_id(request)),
        daemon=True,
    )
    thread.start()
    return {"status": "started", "pack": packs_csv}


@app.get("/models/pull/status")
async def models_pull_status(_: None = Depends(verify_token)):
    """Poll pack pull progress. Auth required."""
    with _pull_lock:
        return dict(_pull_status)


def _run_gguf_pull(repos_csv: str, correlation_id: str = "") -> None:
    """Run gguf-puller (docker compose --profile models). Empty repos_csv uses GGUF_MODELS from project .env."""
    label = repos_csv.strip() or "(GGUF_MODELS from .env)"
    with _gguf_pull_lock:
        _gguf_pull_status.update(
            {
                "running": True,
                "output": f"Starting gguf-puller for {label}…\n",
                "done": False,
                "success": None,
                "repos": label,
            }
        )
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["--profile", "models", "run", "--rm"]
    if repos_csv.strip():
        cmd += ["-e", f"GGUF_MODELS={repos_csv.strip()}"]
    cmd += ["gguf-puller"]
    env = {
        **os.environ,
        "BASE_PATH": BASE_PATH,
        "DATA_PATH": os.environ.get("DATA_PATH", BASE_PATH + "/data"),
    }
    if HF_TOKEN:
        env["HF_TOKEN"] = HF_TOKEN
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd="/workspace",
            env=env,
        )
        output_lines: list[str] = []
        for line in proc.stdout:
            output_lines.append(line.rstrip())
            with _gguf_pull_lock:
                _gguf_pull_status["output"] = "\n".join(output_lines[-40:])
        proc.wait(timeout=7200)
        ok = proc.returncode == 0
        _audit("gguf_pull", label, "ok" if ok else "error", f"exit={proc.returncode}", correlation_id=correlation_id)
        with _gguf_pull_lock:
            _gguf_pull_status["success"] = ok
            _gguf_pull_status["output"] = "\n".join(output_lines[-50:])
            if not ok:
                _gguf_pull_status["output"] += f"\nExit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("GGUF pull timed out after 7200s")
        _audit("gguf_pull", label, "error", "timeout after 7200s", correlation_id=correlation_id)
        with _gguf_pull_lock:
            _gguf_pull_status["success"] = False
            _gguf_pull_status["output"] += "\nError: timed out after 2 hours"
    except Exception as e:
        logger.error("GGUF pull failed: %s", e)
        _audit("gguf_pull", label, "error", str(e)[:200], correlation_id=correlation_id)
        with _gguf_pull_lock:
            _gguf_pull_status["success"] = False
            _gguf_pull_status["output"] += f"\nError: {e}"
    finally:
        with _gguf_pull_lock:
            _gguf_pull_status["running"] = False
            _gguf_pull_status["done"] = True


class GgufPullRequest(BaseModel):
    """Comma-separated Hugging Face repo ids (e.g. org/model-GGUF). Empty uses .env GGUF_MODELS."""

    repos: str = ""
    confirm: bool = False


@app.post("/models/gguf-pull")
async def models_gguf_pull(body: GgufPullRequest, request: Request, _: None = Depends(verify_token)):
    """Run gguf-puller container. Auth required."""
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    raw = (body.repos or "").strip()
    if raw:
        for part in raw.split(","):
            p = part.strip()
            if not p or ".." in p or "/" not in p:
                raise HTTPException(status_code=400, detail=f"Invalid repo segment: {part!r}")
            a, b = p.split("/", 1)
            if not a or not b or "/" in b:
                raise HTTPException(status_code=400, detail=f"Invalid Hugging Face repo id: {p!r}")
    with _gguf_pull_lock:
        if _gguf_pull_status.get("running"):
            raise HTTPException(status_code=409, detail="A GGUF pull is already in progress")
    thread = threading.Thread(
        target=_run_gguf_pull,
        args=(raw, _correlation_id(request)),
        daemon=True,
    )
    thread.start()
    return {"status": "started", "repos": raw or "(from .env)"}


@app.get("/models/gguf-pull/status")
async def models_gguf_pull_status(_: None = Depends(verify_token)):
    """Poll GGUF pull progress. Auth required."""
    with _gguf_pull_lock:
        return dict(_gguf_pull_status)


# --- ComfyUI guardian --------------------------------------------------------

def _comfyui_queue_depth() -> tuple[int, int] | None:
    """Return (running, pending) from ComfyUI /queue, or None if unreachable."""
    try:
        r = httpx.get(f"{COMFYUI_URL}/queue", timeout=3.0)
        r.raise_for_status()
        data = r.json()
        return (len(data.get("queue_running") or []), len(data.get("queue_pending") or []))
    except Exception as e:
        logger.debug("ComfyUI queue poll failed: %s", e)
        return None


def _guardian_transition(new_state: str, error: str = "") -> None:
    with _guardian_lock:
        _guardian_status["state"] = new_state
        _guardian_status["last_transition"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if error:
            _guardian_status["last_error"] = error[:200]


def _guardian_loop() -> None:
    """Poll ComfyUI queue; stop the target service when non-empty, start after drain."""
    target = COMFYUI_GUARDIAN_TARGET
    drain_started: float | None = None
    _guardian_transition("idle")
    print(f"[guardian] loop started target={target}", flush=True)

    while True:
        try:
            depth = _comfyui_queue_depth()
            if depth is None:
                with _guardian_lock:
                    _guardian_status["comfyui_queue"] = {"running": 0, "pending": 0, "reachable": False}
                time.sleep(COMFYUI_QUEUE_POLL_SECONDS)
                continue

            running, pending = depth
            busy = (running + pending) > 0
            with _guardian_lock:
                _guardian_status["comfyui_queue"] = {"running": running, "pending": pending, "reachable": True}
                state = _guardian_status["state"]
                paused_by_us = _guardian_status["paused_by_us"]

            if busy and state == "idle":
                containers = _containers_for_service(target)
                if not containers:
                    print(f"[guardian] ERROR: no container for service={target}", flush=True)
                    _guardian_transition("error", f"no container for {target}")
                else:
                    running_containers = [c for c in containers if c.status == "running"]
                    if running_containers:
                        print(f"[guardian] PAUSE {target} (queue running={running} pending={pending})", flush=True)
                        errs: list[str] = []
                        for c in running_containers:
                            try:
                                c.stop(timeout=30)
                            except Exception as e:
                                errs.append(str(e))
                        if errs:
                            print(f"[guardian] PAUSE ERROR: {'; '.join(errs)[:200]}", flush=True)
                            _audit("guardian_pause", target, "error", "; ".join(errs)[:200])
                            _guardian_transition("error", "; ".join(errs))
                        else:
                            print(f"[guardian] {target} stopped", flush=True)
                            _audit("guardian_pause", target, "ok", f"queue running={running} pending={pending}")
                            with _guardian_lock:
                                _guardian_status["paused_by_us"] = True
                            _guardian_transition("paused")
                    else:
                        # Already stopped by something else — we won't auto-resume it
                        with _guardian_lock:
                            _guardian_status["paused_by_us"] = False
                        _guardian_transition("paused")

            elif busy and state == "draining":
                drain_started = None
                _guardian_transition("paused")

            elif not busy and state == "paused":
                drain_started = time.monotonic()
                _guardian_transition("draining")

            elif not busy and state == "draining":
                if drain_started is not None and (time.monotonic() - drain_started) >= COMFYUI_DRAIN_SECONDS:
                    if paused_by_us:
                        print(f"[guardian] RESUME {target} (drain elapsed)", flush=True)
                        containers = _containers_for_service(target)
                        errs = []
                        for c in containers:
                            try:
                                c.start()
                            except Exception as e:
                                errs.append(str(e))
                        if errs:
                            print(f"[guardian] RESUME ERROR: {'; '.join(errs)[:200]}", flush=True)
                            _audit("guardian_resume", target, "error", "; ".join(errs)[:200])
                            _guardian_transition("error", "; ".join(errs))
                            drain_started = None
                            time.sleep(COMFYUI_QUEUE_POLL_SECONDS)
                            continue
                        print(f"[guardian] {target} started", flush=True)
                        _audit("guardian_resume", target, "ok", "drain_elapsed")
                    with _guardian_lock:
                        _guardian_status["paused_by_us"] = False
                    drain_started = None
                    _guardian_transition("idle")

                    # Phase 1: free ComfyUI's PyTorch caching-allocator pool now
                    # that the queue has drained. Without this, post-job VRAM
                    # stays held even when no work is running (PyTorch keeps the
                    # pool warm). Non-fatal — log + continue on failure.
                    if COMFYUI_FREE_AFTER_DRAIN:
                        ok, detail = _call_comfyui_free(reason="post_drain")
                        print(f"[guardian] /free post-drain: ok={ok} {detail}", flush=True)
                        _audit(
                            "guardian_free_after_drain",
                            "comfyui",
                            "ok" if ok else "error",
                            detail,
                        )

            elif state == "error":
                # Try to recover: if queue is empty and we didn't pause, reset to idle
                if not busy:
                    _guardian_transition("idle")
                # else stay in error until queue drains

        except Exception as e:  # noqa: BLE001
            logger.exception("guardian: loop iteration failed")
            _guardian_transition("error", str(e))

        time.sleep(COMFYUI_QUEUE_POLL_SECONDS)


@app.get("/guardian/status")
async def guardian_status(_: None = Depends(verify_token)):
    """Return current ComfyUI-guardian state. Auth required."""
    with _guardian_lock:
        return dict(_guardian_status)


# Start the guardian thread at module import. Doing it here instead of via
# @app.on_event("startup") (deprecated in recent FastAPI) guarantees the thread
# spawns regardless of the app lifecycle and surfaces errors immediately.
if COMFYUI_SERIALIZE_LLAMACPP:
    print(
        f"[guardian] ENABLED target={COMFYUI_GUARDIAN_TARGET} "
        f"poll={COMFYUI_QUEUE_POLL_SECONDS}s drain={COMFYUI_DRAIN_SECONDS}s "
        f"comfyui={COMFYUI_URL}",
        flush=True,
    )
    threading.Thread(target=_guardian_loop, daemon=True, name="comfyui-guardian").start()
else:
    print("[guardian] disabled (set COMFYUI_SERIALIZE_LLAMACPP=1 to enable)", flush=True)


def _vram_pressure_watchdog_loop() -> None:
    """Phase 2: proactively call ComfyUI /free when VRAM use exceeds threshold.

    Independent of the guardian's queue-state machine. Polls total GPU memory
    every OPS_VRAM_POLL_SECONDS; on exceed of OPS_VRAM_PRESSURE_GB, calls /free
    until used falls below OPS_VRAM_RECOVERY_GB (or pressure-4 if unset).
    """
    threshold = OPS_VRAM_PRESSURE_GB
    recovery = OPS_VRAM_RECOVERY_GB if OPS_VRAM_RECOVERY_GB > 0 else max(threshold - 4, 0.0)
    print(
        f"[vram-watchdog] enabled threshold={threshold}GB recovery={recovery}GB "
        f"poll={OPS_VRAM_POLL_SECONDS}s",
        flush=True,
    )
    while True:
        try:
            used = _read_total_vram_used_gb()
            if used is None:
                # NVML not available (WSL2/WDDM/no GPU) — back off, retry slowly
                time.sleep(max(OPS_VRAM_POLL_SECONDS, 60))
                continue
            if used >= threshold:
                ok, detail = _call_comfyui_free(reason=f"pressure used={used:.1f}GB threshold={threshold}GB")
                _audit(
                    "vram_pressure_acted",
                    "comfyui",
                    "ok" if ok else "error",
                    f"used_gb={used:.1f} threshold_gb={threshold} target_gb={recovery} {detail}",
                )
                # Wait a bit for the OS to actually reclaim before re-measuring.
                time.sleep(5)
        except Exception:
            logger.exception("[vram-watchdog] iteration crashed")
        time.sleep(OPS_VRAM_POLL_SECONDS)


if OPS_VRAM_PRESSURE_GB > 0:
    threading.Thread(target=_vram_pressure_watchdog_loop, daemon=True, name="vram-pressure-watchdog").start()
else:
    print("[vram-watchdog] disabled (set OPS_VRAM_PRESSURE_GB > 0 to enable)", flush=True)


# ── /registry/* endpoints (Tasks 5–9) ────────────────────────────────────────


class RegistryAssignBody(BaseModel):
    gpu_uuid: str
    confirm: bool = False
    force: bool = False


@app.get("/registry/models")
async def registry_list_models(_: None = Depends(verify_token)):
    """List all models in the registry."""
    return {"models": {mid: rec.model_dump() for mid, rec in REGISTRY.list_models().items()}}


@app.get("/registry/models/{model_id}")
async def registry_get_model(model_id: str, _: None = Depends(verify_token)):
    """Get a single model record by ID. 404 if not found."""
    rec = REGISTRY.get(model_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} not found")
    return rec.model_dump()


@app.post("/registry/models")
async def registry_define_model(
    record: model_registry.ModelRecord,
    request: Request,
    _: None = Depends(verify_token),
):
    """Upsert a model record into the registry."""
    record.updated_by = request.headers.get("X-Actor", "dashboard")
    REGISTRY.upsert(record)
    _audit("registry_define", record.id, "ok", correlation_id=_correlation_id(request))
    return record.model_dump()


@app.delete("/registry/models/{model_id}")
async def registry_delete_model(
    model_id: str,
    request: Request,
    _: None = Depends(verify_token),
):
    """Delete a model record. 404 if not found."""
    if REGISTRY.get(model_id) is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} not found")
    REGISTRY.delete(model_id)
    _audit("registry_delete", model_id, "ok", correlation_id=_correlation_id(request))
    return {"ok": True, "id": model_id}


@app.post("/registry/models/{model_id}/assign-gpu")
async def registry_assign_gpu(
    model_id: str,
    body: RegistryAssignBody,
    request: Request,
    _: None = Depends(verify_token),
):
    """Pin a model to a GPU UUID, update gpu-assignments.yml, and recreate the service."""
    rec = REGISTRY.get(model_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} not found")
    if not _GPU_UUID_RE.fullmatch(body.gpu_uuid):
        raise HTTPException(status_code=400, detail="Invalid GPU UUID format")
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.",
        )
    # Capacity guard (skip when force=True)
    # Exclude the record itself so reassigning to the same GPU doesn't double-count.
    if not body.force:
        others = [m for m in REGISTRY.list_models().values() if m.id != rec.id]
        fits, used, total = model_registry.capacity_check(
            _live_gpus(), body.gpu_uuid, others, rec.est_vram_gb,
        )
        if not fits:
            raise HTTPException(
                status_code=409,
                detail=f"VRAM insufficient: {used + rec.est_vram_gb:.1f} GB requested, {total:.1f} GB available on {body.gpu_uuid}. Use force=true to override.",
            )
    # Save previous pin for rollback
    prev_uuid = rec.gpu_uuid
    rec.gpu_uuid = body.gpu_uuid
    rec.updated_by = request.headers.get("X-Actor", "dashboard")
    REGISTRY.upsert(rec)
    # Rewrite gpu-assignments.yml
    assignments = {m.service: m.gpu_uuid for m in REGISTRY.list_models().values() if m.gpu_uuid}
    try:
        _write_text_atomic(GPU_ASSIGNMENTS_PATH, model_registry.render_gpu_assignments_yaml(assignments))
    except Exception as exc:
        # Rollback registry
        rec.gpu_uuid = prev_uuid
        REGISTRY.upsert(rec)
        _audit("registry_assign_gpu", model_id, "error", str(exc)[:200], correlation_id=_correlation_id(request))
        raise HTTPException(status_code=500, detail=f"Failed to write gpu-assignments.yml: {exc}") from exc
    # Recreate service
    try:
        _recreate_service(rec.service, request)
    except HTTPException as exc:
        # Rollback both registry and YAML
        rec.gpu_uuid = prev_uuid
        REGISTRY.upsert(rec)
        prev_assignments = {m.service: m.gpu_uuid for m in REGISTRY.list_models().values() if m.gpu_uuid}
        try:
            _write_text_atomic(GPU_ASSIGNMENTS_PATH, model_registry.render_gpu_assignments_yaml(prev_assignments))
        except Exception:
            pass
        _audit("registry_assign_gpu", model_id, "error", str(exc.detail)[:200], correlation_id=_correlation_id(request))
        raise
    _audit("registry_assign_gpu", model_id, "ok", body.gpu_uuid, correlation_id=_correlation_id(request))
    return {"ok": True, "id": model_id, "gpu_uuid": body.gpu_uuid}


@app.post("/registry/models/{model_id}/enable")
async def registry_enable_model(
    model_id: str,
    body: ConfirmBody,
    request: Request,
    _: None = Depends(verify_token),
):
    """Activate a model: deactivate siblings on the same service, write env, recreate."""
    rec = REGISTRY.get(model_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} not found")
    if rec.runtime != "single-model":
        raise HTTPException(status_code=400, detail=f"Model {model_id!r} has runtime={rec.runtime!r}; only single-model records can be enabled via this endpoint")
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.",
        )
    # Capture previous enabled states for rollback
    prev_target_enabled = rec.enabled
    prev_sibling_states: dict[str, bool] = {}
    prev_active_record = None
    for mid, sibling in REGISTRY.list_models().items():
        if mid != model_id and sibling.service == rec.service and sibling.enabled:
            prev_sibling_states[mid] = True
            if prev_active_record is None:
                prev_active_record = sibling
    # Capture the current values of env keys we are about to overwrite, so we
    # can restore them on recreate failure even when no sibling was previously
    # active (the old code only restored env when prev_active_record existed).
    new_env = REGISTRY.derive_env(rec)
    prior_env = model_registry._parse_env(REGISTRY.env_path)
    prev_for_keys = {k: prior_env[k] for k in new_env if k in prior_env}

    def _rollback_enable(exc: Exception, status: int, detail: str) -> None:
        """Restore registry to pre-mutation state and re-raise as HTTPException."""
        rec.enabled = prev_target_enabled
        REGISTRY.upsert(rec)
        for mid in prev_sibling_states:
            sib = REGISTRY.get(mid)
            if sib is not None:
                sib.enabled = True
                REGISTRY.upsert(sib)
        # Robust env restore: use the keys-we-wrote snapshot first; fall back
        # to deriving from the previously active sibling if no snapshot exists.
        if prev_for_keys:
            try:
                _set_env_keys(prev_for_keys, request)
            except Exception:
                pass
        elif prev_active_record is not None:
            prev_env_derived = REGISTRY.derive_env(prev_active_record)
            if prev_env_derived:
                try:
                    _set_env_keys(prev_env_derived, request)
                except Exception:
                    pass
        _audit("registry_enable", model_id, "error", str(exc)[:200], correlation_id=_correlation_id(request))
        raise HTTPException(status_code=status, detail=detail) from exc

    # Deactivate siblings on the same service
    for mid in prev_sibling_states:
        sibling = REGISTRY.get(mid)
        if sibling is not None:
            sibling.enabled = False
            REGISTRY.upsert(sibling)
    # Activate target
    rec.enabled = True
    REGISTRY.upsert(rec)
    # Push derived env keys — rollback registry on any failure (incl. newline injection 400)
    if new_env:
        try:
            _set_env_keys(new_env, request)
        except HTTPException as exc:
            _rollback_enable(exc, exc.status_code, exc.detail)
        except Exception as exc:
            _rollback_enable(exc, 500, f"Env write failed: {exc}")
    # Recreate service so new env takes effect — rollback registry on failure
    try:
        _recreate_service(rec.service, request)
    except Exception as exc:
        _rollback_enable(exc, 500, f"Service recreate failed: {exc}")
    _audit("registry_enable", model_id, "ok", correlation_id=_correlation_id(request))
    return {"ok": True, "id": model_id, "enabled": True}


@app.get("/registry/gpus")
async def registry_gpus(_: None = Depends(verify_token)):
    """Live GPU info merged with registry model assignments."""
    live = _live_gpus()
    models = REGISTRY.list_models()
    # Build uuid -> list of model ids
    uuid_to_models: dict = {}
    for mid, m in models.items():
        if m.gpu_uuid:
            uuid_to_models.setdefault(m.gpu_uuid, []).append(mid)
    result: dict = {}
    for uuid, info in live.items():
        result[uuid] = {**info, "models": uuid_to_models.get(uuid, [])}
    return {"gpus": result}


# ── FastAPI lifespan: start watchdog task ────────────────────────────────────
async def _startup_watchdog() -> None:
    """Start the Hermes self-heal watchdog if enabled."""
    global _WATCHDOG_TASK
    if OPS_HERMES_WATCHDOG_ENABLED:
        logger.info("[watchdog] ENABLED interval=%ss grace=%ss pause=%s",
                     OPS_HERMES_WATCHDOG_INTERVAL_SECONDS,
                     OPS_HERMES_WATCHDOG_GRACE_SECONDS,
                     OPS_HERMES_WATCHDOG_PAUSE_FILE)
        _WATCHDOG_TASK = asyncio.create_task(_hermes_watchdog_loop(), name="hermes-watchdog")
    else:
        logger.info("[watchdog] disabled (set OPS_HERMES_WATCHDOG_ENABLED=1 to enable)")


@app.on_event("startup")
async def _startup() -> None:
    await _startup_watchdog()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _WATCHDOG_TASK and not _WATCHDOG_TASK.done():
        _WATCHDOG_TASK.cancel()
        try:
            await _WATCHDOG_TASK
        except (asyncio.CancelledError, RuntimeError):
            pass
        logger.info("[watchdog] cancelled on shutdown")
