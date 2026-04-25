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
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import docker
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

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

app = FastAPI(title="Ops Controller", version="1.0.0")
logger = logging.getLogger(__name__)

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "ordo-ai-stack")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "")
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.log"))
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", "10485760"))  # 10MB default

# Services we allow operations on (allowlist)
ALLOWED_SERVICES = {
    "llamacpp", "llamacpp-embed", "dashboard", "open-webui", "model-gateway", "mcp-gateway",
    "comfyui", "n8n", "qdrant",
}

# .env keys we allow updating via the API
ENV_ALLOWED_KEYS = {
    "DEFAULT_MODEL",
    "OPEN_WEBUI_DEFAULT_MODEL",
    "LLAMACPP_MODEL",
    "LLAMACPP_FLASH_ATTN",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION",
    "LLAMACPP_KV_CACHE_TYPE_K",
    "LLAMACPP_KV_CACHE_TYPE_V",
    "LLAMACPP_EXTRA_ARGS",
}

BASE_PATH = os.environ.get("BASE_PATH", ".")
COMPOSE_FILE_ENV = os.environ.get("COMPOSE_FILE", "docker-compose.yml")

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
        online_cpus = int(cpu.get("online_cpus") or len((cpu["cpu_usage"].get("percpu_usage") or [])) or 1)
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


@app.get("/health")
async def health():
    """Controller health. No auth required. Verifies Docker daemon reachable."""
    try:
        _docker_client().ping()
    except Exception as e:
        logger.warning("Health check failed: %s", e)
        return JSONResponse(status_code=503, content={"ok": False, "error": "Docker daemon unavailable"})
    return {"ok": True}


@app.get("/services")
async def list_services():
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
async def list_containers(_: None = Depends(verify_token)):
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
    containers = _containers_for_service(service_id)
    if not containers:
        raise HTTPException(status_code=404, detail=f"No container found for service {service_id}")
    errs = []
    for c in containers:
        try:
            c.restart(timeout=30)
        except Exception as e:
            errs.append(str(e))
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


@app.get("/mcp/containers")
async def mcp_containers(_: None = Depends(verify_token)):
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
async def stats_services(_: None = Depends(verify_token)):
    """Per-compose-service CPU/RAM/VRAM. Read-only, auth required (same as other ops routes)."""
    try:
        containers = _get_containers()
    except Exception as e:
        logger.warning("stats/services: docker list failed: %s", e)
        return {"gpu": None, "services": {}, "vram_aggregate_unavailable": True}

    vram_by_pid, gpu = await asyncio.to_thread(_nvml_vraam_by_pid)
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
