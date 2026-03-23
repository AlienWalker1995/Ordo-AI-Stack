"""Ops Controller — secure Docker Compose control plane."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import docker
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI(title="Ops Controller", version="1.0.0")
logger = logging.getLogger(__name__)

COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT", "ai-toolkit")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "")
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.log"))
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", "10485760"))  # 10MB default

# Services we allow operations on (allowlist)
ALLOWED_SERVICES = {
    "ollama", "dashboard", "open-webui", "model-gateway", "mcp-gateway",
    "comfyui", "n8n", "openclaw-gateway", "qdrant",
}

# .env keys we allow updating via the API
ENV_ALLOWED_KEYS = {"DEFAULT_MODEL"}

BASE_PATH = os.environ.get("BASE_PATH", ".")
COMPOSE_FILE_ENV = os.environ.get("COMPOSE_FILE", "docker-compose.yml")

# Model download (ComfyUI files)
COMFYUI_MODELS_DIR = Path(os.environ.get("COMFYUI_MODELS_DIR", "/models/comfyui"))
# Same layout as docker-compose: ${BASE_PATH}/data/comfyui-storage → comfyui /root
COMFYUI_CUSTOM_NODES_DIR = Path("/workspace/data/comfyui-storage/ComfyUI/custom_nodes")
COMFYUI_CONTAINER_NAME = os.environ.get("COMFYUI_CONTAINER_NAME", "comfyui")
COMFYUI_CATEGORIES = ("checkpoints", "loras", "text_encoders", "latent_upscale_models")
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


def _docker_client():
    return docker.from_env()


async def verify_token(request: Request) -> None:
    """Verify Bearer token. Use as Depends(verify_token)."""
    if not OPS_CONTROLLER_TOKEN:
        raise HTTPException(status_code=503, detail="Ops controller not configured (no token)")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = auth[7:].strip()
    if token != OPS_CONTROLLER_TOKEN:
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


@app.get("/health")
async def health():
    """Controller health. No auth required."""
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
        return {"services": [], "error": str(e)}


class ConfirmBody(BaseModel):
    confirm: bool = False
    dry_run: bool = False


def _correlation_id(request: Request) -> str:
    """Extract X-Request-ID for audit correlation."""
    return (request.headers.get("X-Request-ID") or "").strip()


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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
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
    tail_n = min(tail, 500)
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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
    if body.key not in ENV_ALLOWED_KEYS:
        raise HTTPException(status_code=400, detail=f"Key not in allowlist: {body.key!r}")
    if "\n" in body.value or "\r" in body.value:
        raise HTTPException(status_code=400, detail="Value must not contain newlines")
    env_path = Path("/workspace/.env")
    if not env_path.exists():
        raise HTTPException(status_code=404, detail=".env not found at /workspace/.env")
    content = env_path.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(body.key)}=.*"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{body.key}={body.value}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{body.key}={body.value}\n"
    env_path.write_text(content, encoding="utf-8")
    _audit("env_set", body.key, "ok", body.value[:80], correlation_id=_correlation_id(request))
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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["up", "-d", "--no-deps", service_id]
    env = {**os.environ, "BASE_PATH": BASE_PATH}
    result = subprocess.run(cmd, capture_output=True, text=True, cwd="/workspace", env=env)
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
    lines = AUDIT_LOG_PATH.read_text().strip().splitlines()
    entries = []
    for line in reversed(lines[-limit:]):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": list(reversed(entries))}


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
        er = container.exec_run(["pip", "install", "-r", req_container], demux=False)
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
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
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


# --- Model downloads (ComfyUI files) ---


def _auto_detect_category(url: str, filename: str) -> str:
    """Guess ComfyUI model category from URL path or filename."""
    parts = url.lower()
    for cat in COMFYUI_CATEGORIES:
        if cat in parts:
            return cat
    fn = filename.lower()
    if "lora" in fn or "lora" in parts:
        return "loras"
    if "text_encoder" in parts or "text_encoder" in fn:
        return "text_encoders"
    if "upscale" in parts or "upscale" in fn:
        return "latent_upscale_models"
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
        req_headers = {"User-Agent": "AI-toolkit/1.0"}
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


def _run_model_pull(pack: str, correlation_id: str = "") -> None:
    """Run comfyui-model-puller via docker compose. Uses the same logic as manual pull (works for gated models)."""
    with _pull_lock:
        _pull_status.update({"running": True, "output": f"Starting pack: {pack}", "done": False, "success": None, "pack": pack})
    compose_files = [f.strip() for f in COMPOSE_FILE_ENV.split(";") if f.strip()]
    cmd = ["docker-compose"]
    for cf in compose_files:
        cmd += ["-f", f"/workspace/{cf}"]
    cmd += ["--profile", "comfyui-models", "run", "--rm", "-e", f"COMFYUI_PACKS={pack}", "comfyui-model-puller"]
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
        proc.wait()
        ok = proc.returncode == 0
        _audit("model_pull", pack, "ok" if ok else "error", f"exit={proc.returncode}", correlation_id=correlation_id)
        with _pull_lock:
            _pull_status["success"] = ok
            _pull_status["output"] = "\n".join(output_lines[-30:])
            if not ok:
                _pull_status["output"] += f"\nExit code: {proc.returncode}"
    except Exception as e:
        logger.error("Model pull failed: %s", e)
        _audit("model_pull", pack, "error", str(e)[:200], correlation_id=correlation_id)
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


@app.post("/models/pull")
async def models_pull(body: ModelPullRequest, request: Request, _: None = Depends(verify_token)):
    """Run comfyui-model-puller for a pack (e.g. flux1-dev). Works for gated models. Auth required."""
    pack = body.pack.strip().lower()
    if not pack:
        raise HTTPException(status_code=400, detail="pack is required")
    valid = _valid_packs()
    if pack not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown pack. Valid: {', '.join(sorted(valid))}")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
    with _pull_lock:
        if _pull_status.get("running"):
            raise HTTPException(status_code=409, detail="A pull is already in progress")
    thread = threading.Thread(
        target=_run_model_pull,
        args=(pack, _correlation_id(request)),
        daemon=True,
    )
    thread.start()
    return {"status": "started", "pack": pack}


@app.get("/models/pull/status")
async def models_pull_status(_: None = Depends(verify_token)):
    """Poll pack pull progress. Auth required."""
    with _pull_lock:
        return dict(_pull_status)
