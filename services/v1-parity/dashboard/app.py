"""Ordo AI Stack Dashboard — unified model management and service hub."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

# Lock for shared mutable state accessed from both async handlers and background threads
_state_lock = threading.Lock()

import psutil
import yaml

logger = logging.getLogger(__name__)

import httpx as _httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from dashboard import gpu_stats, settings
from dashboard import routes_gpu as _routes_gpu
from dashboard import routes_model_config as _routes_model_config
from dashboard import routes_registry as _routes_registry
from dashboard.orchestration_db import get_job_counts, get_outbox_stats
from dashboard.routes_hub import router as hub_router
from dashboard.routes_orchestration import router as orchestration_router
from dashboard.services_catalog import OPS_SERVICE_MAP
from dashboard.settings import AUTH_REQUIRED as _AUTH_REQUIRED
from dashboard.settings import DASHBOARD_AUTH_TOKEN


async def _read_json_async(path: Path) -> dict:
    """Read and parse a JSON file off the event loop."""
    return await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))


async def _write_json_async(path: Path, data: dict) -> None:
    """Serialise and write JSON off the event loop via atomic write-then-rename."""
    def _atomic_write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    await asyncio.to_thread(_atomic_write)


# Persistent httpx client — connection pooling avoids per-request TCP handshake overhead.
_http_client: _httpx.AsyncClient | None = None


def _get_http_client() -> _httpx.AsyncClient:
    """Return the shared async HTTP client (created in lifespan)."""
    assert _http_client is not None, "HTTP client not initialised — is lifespan running?"
    return _http_client

# Dashboard auth (optional bearer token only; see dashboard.settings)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http_client
    if not _AUTH_REQUIRED:
        logger.warning(
            "Dashboard is running WITHOUT authentication. "
            "Set DASHBOARD_AUTH_TOKEN in .env to require Bearer auth on /api/*."
        )
    _http_client = _httpx.AsyncClient(
        timeout=30.0,
        limits=_httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None


app = FastAPI(title="Ordo AI Stack Dashboard", version="1.0.0", lifespan=_lifespan)
app.include_router(hub_router)
app.include_router(orchestration_router)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions — log the traceback but return a safe 500 to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _request_from_trusted_proxy(request: Request) -> bool:
    """True if the request originates from the configured proxy network."""
    if not settings.DASHBOARD_TRUST_PROXY_HEADERS:
        return False
    if settings.DASHBOARD_TRUSTED_PROXY_NET is None:
        return False
    client_ip = request.client.host if request.client else None
    if client_ip is None:
        return False
    try:
        return ipaddress.ip_address(client_ip) in settings.DASHBOARD_TRUSTED_PROXY_NET
    except ValueError:
        return False


def _verify_auth(request: Request) -> bool | str:
    """Verify the request's authentication.

    Order of precedence:
      1. Trusted-proxy branch — if the request originates from the configured
         proxy network and carries an X-Forwarded-Email header, accept it.
         If the proxy is trusted but no email is present, fail closed when
         AUTH_REQUIRED so a misconfigured proxy can't silently bypass auth.
      2. Bearer-token branch — Authorization: Bearer <DASHBOARD_AUTH_TOKEN>
         (preserved for orchestration-mcp / internal callers).

    Returns a truthy value when auth passes (the email or True for bearer),
    False when auth fails. Returns True when auth is not required.
    """
    if _request_from_trusted_proxy(request):
        email = request.headers.get("X-Forwarded-Email", "").strip()
        if email:
            return email
        # Trusted proxy connected but no identity header — refuse rather
        # than silently bypass auth (fail-closed).
        return not _AUTH_REQUIRED

    if not _AUTH_REQUIRED:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token, DASHBOARD_AUTH_TOKEN)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add CSP and security headers to reduce XSS token theft risk."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "  # unsafe-inline: React emits inline style={{…}}
        "font-src 'self'; "
        "script-src 'self'; "  # Vite build emits only external hashed ES modules — no inline scripts
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require auth for /api/* except health/hub read-only endpoints."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    if path in (
        "/api/health",
        "/api/dependencies",
        "/api/auth/config",
        "/api/hardware",
        "/api/throughput/stats",
        "/api/throughput/service-usage",
        "/api/rag/status",
        "/api/orchestration/readiness",
    ):
        return await call_next(request)
    # /api/throughput/record: requires THROUGHPUT_RECORD_TOKEN when set (model-gateway internal; PRD §3.E)
    if path == "/api/throughput/record":
        token = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
        if token and not hmac.compare_digest(request.headers.get("X-Throughput-Token", ""), token):
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-Throughput-Token"})
        return await call_next(request)
    if _AUTH_REQUIRED and not _verify_auth(request):
        logger.warning(
            "AUTH_FAIL path=%s method=%s src=%s",
            path, request.method,
            request.client.host if request.client else "unknown",
        )
        return JSONResponse(status_code=401, content={"detail": "Bearer token required"})
    return await call_next(request)


# GZip compression for text responses (JSON payloads, the HTML shell, JS/CSS assets).
# add_middleware inserts at the outermost position, so it wraps the auth/security http
# middlewares and compresses the final response body after they run. minimum_size=500
# skips tiny payloads (401 bodies, small JSON) where compression is net-negative.
# There are NO streaming/SSE endpoints in this app (grep: no StreamingResponse /
# EventSourceResponse / text/event-stream; the only `yield` is the lifespan cm), so
# response buffering is not a concern; Starlette's GZipMiddleware also streams-compresses
# StreamingResponse chunk-by-chunk rather than buffering, were one ever added.
app.add_middleware(GZipMiddleware, minimum_size=500)


MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435").rstrip("/")
MODEL_GATEWAY_API_KEY = (os.environ.get("MODEL_GATEWAY_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "")).strip()
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))


def _model_gateway_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if MODEL_GATEWAY_API_KEY:
        headers["Authorization"] = f"Bearer {MODEL_GATEWAY_API_KEY}"
    return headers

# Background pull status dicts
_comfyui_status: dict = {"running": False, "output": "", "done": False, "success": None}
_gguf_pull_status: dict = {"running": False, "model": "", "output": "", "pct": 0, "done": False, "success": None}



class PullRequest(BaseModel):
    model: str


# --- LLM (llama.cpp / GGUF) ---


_GGUF_MODELS_DIR = Path(os.environ.get("GGUF_MODELS_DIR", "/gguf-models"))


def _scan_gguf_models() -> list[dict]:
    """Return all .gguf files on disk with their sizes."""
    models = []
    try:
        for p in sorted(_GGUF_MODELS_DIR.iterdir()):
            if p.suffix.lower() == ".gguf" and p.is_file():
                st = p.stat()
                models.append({"name": p.name, "size": st.st_size, "modified_at": int(st.st_mtime)})
    except OSError as e:
        logger.warning("GGUF model scan failed: %s", e)
    return models


@app.get("/api/llm/models")
async def llm_models():
    """List GGUF models available on disk (primary) merged with gateway active-model info."""
    disk_models = await asyncio.to_thread(_scan_gguf_models)
    if disk_models:
        return {"models": disk_models, "ok": True}
    # Fallback: ask model-gateway
    try:
        r = await _get_http_client().get(f"{MODEL_GATEWAY_URL}/v1/models", headers=_model_gateway_headers())
        r.raise_for_status()
        data = r.json()
        models = [{"name": m["id"]} for m in data.get("data", []) if m.get("id")]
        return {"models": models, "ok": True}
    except Exception as e:
        return {"models": [], "ok": False, "error": str(e)}


@app.post("/api/llm/delete")
async def llm_delete(req: PullRequest):
    """Delete a GGUF model file from disk."""
    name = (req.model or "").strip()
    if not name or ".." in name or "/" in name:
        raise HTTPException(status_code=400, detail="Invalid model name")
    if not name.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Model must be a .gguf filename")
    path = (_GGUF_MODELS_DIR / name).resolve()
    try:
        path.relative_to(_GGUF_MODELS_DIR.resolve())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid model path") from e
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found on disk")
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot delete model: {e}") from e
    logger.info("MODEL_DELETED model=%s path=%s", name, path)
    return {"ok": True, "message": f"Deleted '{name}' from disk."}


@app.post("/api/llm/unload")
async def llm_unload(req: PullRequest):
    """501 — Ollama-era relic. LiteLLM has no /api/delete; llama.cpp is the sole backend
    (Ollama decommissioned 2026-07-01), so this could only ever 404/502 (audit P2-35).
    Model lifecycle is the scheduler's job: switch models via the ops-controller
    /model-config path (dashboard Model Control), which re-renders and recreates llamacpp."""
    raise HTTPException(
        status_code=501,
        detail=(
            "Unload is not a gateway operation: llama.cpp serves one active model, managed by "
            "the render pipeline. Switch models via Model Control (/model-config) instead."
        ),
    )

@app.post("/api/llamacpp/switch")
async def llamacpp_switch_model(req: PullRequest, request: Request):
    """Switch the active llamacpp model: writes LLAMACPP_MODEL to .env via ops-controller, then recreates llamacpp."""
    model = (req.model or "").strip()
    if not model or ".." in model or "/" in model:
        raise HTTPException(status_code=400, detail="Invalid model filename")
    if not model.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Model must be a .gguf filename")

    # 1. Update LLAMACPP_MODEL in .env
    code, data = await _ops_request(
        "POST", "/env/set", request=request,
        json={"key": "LLAMACPP_MODEL", "value": model, "confirm": True},
    )
    if code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Failed to update .env: {data}")

    # 2. Recreate llamacpp so the new env var takes effect
    code2, data2 = await _ops_request(
        "POST", "/services/llamacpp/recreate", request=request,
        json={"confirm": True},
    )
    started = code2 in (200, 201, 202)
    return {"ok": True, "model": model, "llamacpp_restarting": started}


_model_switch_lock = asyncio.Lock()


@app.post("/api/active-model")
async def set_active_model(req: PullRequest, request: Request):
    """Switch the active llamacpp model. All consumers use the canonical 'local-chat' alias."""
    if _model_switch_lock.locked():
        raise HTTPException(status_code=409, detail="Model switch already in progress")
    async with _model_switch_lock:
        return await _do_set_active_model(req, request)


async def _do_set_active_model(req: PullRequest, request: Request):
    model = (req.model or "").strip()
    if not model or ".." in model or "/" in model:
        raise HTTPException(status_code=400, detail="Invalid model filename")
    if not model.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Model must be a .gguf filename")

    bare_name = model[:-5]  # strip .gguf → gateway model id
    if not bare_name:
        raise HTTPException(status_code=400, detail="Invalid model filename")
    results: dict = {}
    errors: list[str] = []

    # Switch LLAMACPP_MODEL + recreate llamacpp. Every consumer uses the
    # canonical 'local-chat' alias from the model-gateway, so there's nothing
    # else to update.
    code, data = await _ops_request(
        "POST", "/env/set", request=request,
        json={"key": "LLAMACPP_MODEL", "value": model, "confirm": True},
    )
    if code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Failed to update LLAMACPP_MODEL: {data}")
    code2, _ = await _ops_request(
        "POST", "/services/llamacpp/recreate", request=request, json={"confirm": True}
    )
    results["llamacpp_restarting"] = code2 in (200, 201, 202)
    if not results["llamacpp_restarting"]:
        errors.append("llamacpp recreate failed")

    all_ok = len(errors) == 0
    if errors:
        logger.warning("Model switch to %s partial failure: %s", model, "; ".join(errors))
    return {"ok": all_ok, "model": model, "errors": errors, **results}


def _run_gguf_pull(model: str):
    """Download GGUFs via ops-controller gguf-puller (docker compose --profile models)."""
    global _gguf_pull_status
    with _state_lock:
        _gguf_pull_status = {"running": True, "model": model, "output": "", "pct": 0, "done": False, "success": None}

    repos = _normalize_gguf_pull_repos(model)
    if repos is None:
        repos = _normalize_gguf_pull_repos(_hf_url_to_repo(model))
    if repos is None:
        msg = (
            "This stack pulls GGUF files (llama.cpp) directly from Hugging Face.\n\n"
            "Enter a Hugging Face repo id (e.g. bartowski/Llama-3.2-3B-Instruct-GGUF), "
            "a huggingface.co/… page or .gguf URL, hf.co/owner/repo, or type .env to pull all "
            "repos listed in GGUF_MODELS in your .env.\n\n"
            "Bare tag names like llama3.2:8b are not supported; use a Hugging Face repo id or .gguf URL."
        )
        with _state_lock:
            _gguf_pull_status["output"] = msg
            _gguf_pull_status["success"] = False
            _gguf_pull_status["running"] = False
            _gguf_pull_status["done"] = True
        return

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    if not token:
        with _state_lock:
            _gguf_pull_status["output"] = "OPS_CONTROLLER_TOKEN is not set; cannot run gguf-puller from the dashboard."
            _gguf_pull_status["success"] = False
            _gguf_pull_status["running"] = False
            _gguf_pull_status["done"] = True
        return

    try:
        import httpx as _httpx
        with _httpx.Client(timeout=120.0) as client:
            r = client.post(
                f"{ops_url}/models/gguf-pull",
                headers={"Authorization": f"Bearer {token}"},
                json={"repos": repos, "confirm": True},
            )
            if r.status_code == 409:
                with _state_lock:
                    _gguf_pull_status["output"] = "Another model or GGUF pull is already in progress."
                    _gguf_pull_status["success"] = False
                    _gguf_pull_status["running"] = False
                    _gguf_pull_status["done"] = True
                return
            if r.status_code >= 400:
                try:
                    det = r.json().get("detail", r.text)
                except (ValueError, UnicodeDecodeError):
                    det = r.text
                with _state_lock:
                    _gguf_pull_status["output"] = f"Failed to start gguf-puller: {det}"
                    _gguf_pull_status["success"] = False
                    _gguf_pull_status["running"] = False
                    _gguf_pull_status["done"] = True
                return

        deadline = time.time() + 7200  # 2-hour max
        consecutive_errors = 0
        with _httpx.Client(timeout=60.0) as poll_client:
            while time.time() < deadline:
                time.sleep(1.5)
                try:
                    sr = poll_client.get(
                        f"{ops_url}/models/gguf-pull/status",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if sr.status_code != 200:
                        consecutive_errors += 1
                        if consecutive_errors >= 20:
                            raise RuntimeError(f"Poll returned {sr.status_code} 20 times in a row")
                        continue
                    consecutive_errors = 0
                    st = sr.json()
                except Exception as poll_err:
                    consecutive_errors += 1
                    if consecutive_errors >= 20:
                        raise RuntimeError(f"Poll failed 20 times: {poll_err}")
                    continue
                with _state_lock:
                    _gguf_pull_status["output"] = st.get("output", "")
                    _gguf_pull_status["pct"] = 50 if st.get("running") else 100
                if st.get("done"):
                    with _state_lock:
                        _gguf_pull_status["success"] = bool(st.get("success"))
                        _gguf_pull_status["running"] = False
                        _gguf_pull_status["done"] = True
                    break
            else:
                raise TimeoutError("GGUF pull timed out after 2 hours")
    except Exception as e:
        logger.error("GGUF pull failed: %s", e)
        with _state_lock:
            _gguf_pull_status["output"] = (_gguf_pull_status.get("output") or "") + f"\nError: {e}"
            _gguf_pull_status["success"] = False
            _gguf_pull_status["running"] = False
            _gguf_pull_status["done"] = True


@app.post("/api/llm/pull")
async def llm_pull(req: PullRequest):
    """Start GGUF download (gguf-puller via ops-controller) in background. Poll /api/llm/pull/status."""
    global _gguf_pull_status
    with _state_lock:
        if _gguf_pull_status.get("running"):
            raise HTTPException(status_code=409, detail="Pull already in progress")
        _gguf_pull_status["running"] = True
        _gguf_pull_status["model"] = req.model
    thread = threading.Thread(target=_run_gguf_pull, args=(req.model,), daemon=True)
    thread.start()
    return {"status": "started", "model": req.model}


@app.get("/api/llm/pull/status")
async def llm_pull_status():
    """Get GGUF pull progress."""
    with _state_lock:
        return dict(_gguf_pull_status)


# --- ComfyUI ---


def _scan_comfyui_models() -> list[dict]:
    """Scan ComfyUI models directory for installed files."""
    subdirs = COMFYUI_CATEGORIES
    models = []
    for sub in subdirs:
        d = MODELS_DIR / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                size_mb = f.stat().st_size / (1024 * 1024)
                models.append(
                    {
                        "name": f.name,
                        "category": sub,
                        "size_mb": round(size_mb, 1),
                    }
                )
    return sorted(models, key=lambda m: (m["category"], m["name"]))


def _run_comfyui_pull_subprocess(packs: str | None = None):
    """Fallback: run ComfyUI model pull script as subprocess (used when ComfyUI is not running)."""
    script = SCRIPTS_DIR / "comfyui" / "pull_comfyui_models.py"
    env = os.environ.copy()
    env["MODELS_DIR"] = str(MODELS_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    if packs:
        env["COMFYUI_PACKS"] = packs
    proc = None
    try:
        proc = subprocess.Popen(
            ["python3", "-u", str(script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(SCRIPTS_DIR.parent),
        )
        output_lines: list[str] = []
        for line in proc.stdout:
            output_lines.append(line)
            if len(output_lines) > 50:
                output_lines = output_lines[-50:]
            with _state_lock:
                _comfyui_status["output"] = "".join(output_lines)
        proc.wait(timeout=7200)
        with _state_lock:
            _comfyui_status["success"] = proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.error("ComfyUI pull (subprocess) timed out after 7200s")
        with _state_lock:
            _comfyui_status["output"] += "\nError: process timed out after 2 hours"
            _comfyui_status["success"] = False
    except Exception as e:
        logger.error("ComfyUI pull (subprocess) failed: %s", e)
        if proc and proc.poll() is None:
            proc.kill()
        with _state_lock:
            _comfyui_status["output"] += f"\nError: {e}"
            _comfyui_status["success"] = False
    finally:
        with _state_lock:
            _comfyui_status["running"] = False
            _comfyui_status["done"] = True


def _run_comfyui_pull(packs: str | None = None):
    """Pull ComfyUI models from ``models.json``.

    Defaults to **direct HuggingFace download** (``pull_comfyui_models.py``). ComfyUI
    Manager's ``/manager/queue/install_model`` only accepts models that appear in its
    curated ``model-list.json`` (``check_whitelist_for_model`` in Manager); arbitrary
    URLs from our config return **400 Invalid model install request**.

    Set ``COMFYUI_USE_MANAGER_FOR_PULL=1`` to use Manager's queue (only useful if the
    model triple matches Manager's catalog). If ComfyUI is unreachable, falls back to
    direct download when Manager mode was requested.
    """
    import json as _json
    import uuid

    global _comfyui_status
    with _state_lock:
        _comfyui_status = {"running": True, "output": "", "done": False, "success": None}

    use_manager = os.environ.get("COMFYUI_USE_MANAGER_FOR_PULL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if use_manager:
        try:
            urllib.request.urlopen(f"{COMFYUI_URL}/", timeout=5)  # noqa: S310 — internal URL only
        except (OSError, urllib.error.URLError):
            use_manager = False

    if not use_manager:
        with _state_lock:
            _comfyui_status["output"] = (
                "Downloading models directly (ComfyUI Manager only installs its cataloged "
                "models; arbitrary HF URLs get 400 — see dashboard _run_comfyui_pull docstring).\n"
            )
        _run_comfyui_pull_subprocess(packs)
        return

    # Load models config
    config_path = SCRIPTS_DIR / "comfyui" / "models.json"
    try:
        with open(config_path, encoding="utf-8") as f:
            config = _json.load(f)
    except Exception as e:
        with _state_lock:
            _comfyui_status["output"] = f"Failed to read models.json: {e}"
            _comfyui_status["success"] = False
            _comfyui_status["running"] = False
            _comfyui_status["done"] = True
        return

    default_packs = config.get("defaults", {}).get("packs", [])
    default_quant = config.get("defaults", {}).get("quant", "Q4_K_M")
    selected_packs = [p.strip() for p in packs.split(",")] if packs else default_packs
    all_packs = config.get("packs", {})

    # Build list of Manager API requests
    models_to_pull = []
    for pack_name in selected_packs:
        pack = all_packs.get(pack_name)
        if not pack:
            continue
        for model in pack.get("models", []):
            url = model.get("url", "")
            if not url:
                continue
            url = url.replace("{quant}", default_quant)
            raw_file = model["file"].replace("{quant}", default_quant)
            filename = Path(raw_file).name
            models_to_pull.append({
                "ui_id": str(uuid.uuid4()),
                "name": filename,
                "type": model.get("type", model.get("dest", "checkpoints")),
                "base": "other",
                "save_path": model.get("dest", "checkpoints"),
                "description": "",
                "filename": filename,
                "url": url,
                "reference": f"https://huggingface.co/{model['repo']}",
            })

    output_lines: list[str] = []
    _progress_idx: int = -1  # index of replaceable progress block (-1 = none)

    def _append(msg: str, replaceable: bool = False) -> None:
        nonlocal _progress_idx
        if replaceable and _progress_idx >= 0:
            output_lines[_progress_idx] = msg
        else:
            if replaceable:
                _progress_idx = len(output_lines)
            output_lines.append(msg)
        with _state_lock:
            _comfyui_status["output"] = "\n".join(output_lines)

    if not models_to_pull:
        _append("No models with URL found for selected packs.")
        with _state_lock:
            _comfyui_status["success"] = True
            _comfyui_status["running"] = False
            _comfyui_status["done"] = True
        return

    _append(f"Queuing {len(models_to_pull)} model(s) via ComfyUI Manager...")

    try:
        import httpx as _httpx
        with _httpx.Client(timeout=30.0) as client:
            for m in models_to_pull:
                _append(f"  → {m['filename']} ({m['save_path']})")
                r = client.post(f"{COMFYUI_URL}/manager/queue/install_model", json=m)
                if r.status_code not in (200, 201):
                    _append(f"    WARNING: Manager returned {r.status_code}: {r.text[:200]}")

            _append("All models queued. Waiting for downloads to complete...")

            deadline = time.time() + 7200  # 2-hour max
            consecutive_errors = 0
            while time.time() < deadline:
                time.sleep(2)
                try:
                    r = client.get(f"{COMFYUI_URL}/manager/queue/status")
                    data = r.json()
                    consecutive_errors = 0
                except (json.JSONDecodeError, _httpx.RequestError, _httpx.HTTPStatusError) as e:
                    logger.debug("ComfyUI queue poll failed: %s", e)
                    consecutive_errors += 1
                    if consecutive_errors >= 20:
                        raise RuntimeError(f"ComfyUI queue poll failed 20 times: {e}")
                    continue

                items = data if isinstance(data, list) else data.get("queue", [])
                if not items:
                    _append("Download queue empty — done.")
                    break

                done_count = sum(1 for i in items if i.get("status") == "done")
                total = len(items)
                pending = [i for i in items if i.get("status") not in ("done", "error", "failed")]
                progress_parts = [f"Progress: {done_count}/{total} done"]
                for item in pending[:3]:
                    name = item.get("filename") or item.get("name", "?")
                    pct = item.get("progress", 0)
                    progress_parts.append(f"  {name}: {pct}%")
                _append("\n".join(progress_parts), replaceable=True)

                if all(i.get("status") in ("done", "error", "failed") for i in items):
                    errors = [i for i in items if i.get("status") in ("error", "failed")]
                    if errors:
                        _append(f"Completed with {len(errors)} error(s).")
                    else:
                        _append("All downloads complete!")
                    break
            else:
                raise TimeoutError("ComfyUI model pull timed out after 2 hours")

        with _state_lock:
            _comfyui_status["success"] = True
    except Exception as e:
        logger.error("ComfyUI Manager pull failed: %s", e)
        with _state_lock:
            _comfyui_status["output"] += f"\nError: {e}"
            _comfyui_status["success"] = False
    finally:
        with _state_lock:
            _comfyui_status["running"] = False
            _comfyui_status["done"] = True


COMFYUI_CATEGORIES = (
    "checkpoints", "loras", "text_encoders", "latent_upscale_models",
    "vae", "unet", "clip", "clip_vision", "controlnet", "embeddings",
    "upscale_models", "diffusion_models", "vae_approx",
)


@app.delete("/api/comfyui/models/{category}/{filename}")
async def comfyui_delete(category: str, filename: str):
    """Delete a ComfyUI model file. See COMFYUI_CATEGORIES for valid category values."""
    if category not in COMFYUI_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {COMFYUI_CATEGORIES}")
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = MODELS_DIR / category / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{filename}' not found in {category}")
    if not path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    try:
        path.unlink()
        logger.info("MODEL_DELETED model=%s/%s path=%s", category, filename, path)
        return {"ok": True, "message": f"Deleted {category}/{filename}"}
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied: {e}") from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}") from e


@app.get("/api/comfyui/models")
async def comfyui_models():
    """List ComfyUI models on disk."""
    try:
        models = await asyncio.to_thread(_scan_comfyui_models)
        return {"models": models, "ok": True}
    except Exception as e:
        return {"models": [], "ok": False, "error": str(e)}


@app.get("/api/comfyui/packs")
async def comfyui_packs():
    """List available ComfyUI model packs from models.json."""
    config_path = SCRIPTS_DIR / "comfyui" / "models.json"
    if not config_path.exists():
        return {"packs": {}, "defaults": [], "ok": False, "error": "models.json not found"}
    try:
        config = await _read_json_async(config_path)
        default_quant = config.get("defaults", {}).get("quant", "Q4_K_M")
        try:
            models = await asyncio.to_thread(_scan_comfyui_models)
            installed = {(m["category"], m["name"]) for m in models}
        except (OSError, KeyError):
            installed = set()
        packs = {}
        for name, pack in config.get("packs", {}).items():
            models = pack.get("models", [])
            resolved_files = []
            installed_count = 0
            for m in models:
                category = m.get("dest", "checkpoints")
                filename = Path(m["file"].replace("{quant}", default_quant)).name
                resolved_files.append({"category": category, "name": filename})
                if (category, filename) in installed:
                    installed_count += 1

            packs[name] = {
                "description": pack.get("description", ""),
                "capability": pack.get("capability", "other"),
                "model_count": len(models),
                "installed_count": installed_count,
                "files": resolved_files,
            }
        return {"packs": packs, "defaults": config.get("defaults", {}).get("packs", []), "ok": True}
    except Exception as e:
        return {"packs": {}, "defaults": [], "ok": False, "error": str(e)}


@app.post("/api/comfyui/pull")
async def comfyui_pull(packs: str | None = None):
    """Start ComfyUI model pull in background. Optional 'packs' query param (comma-separated pack names)."""
    global _comfyui_status
    with _state_lock:
        if _comfyui_status.get("running"):
            raise HTTPException(status_code=409, detail="Pull already in progress")
        _comfyui_status["running"] = True
    thread = threading.Thread(target=_run_comfyui_pull, args=(packs,))
    thread.daemon = True
    thread.start()
    return {"status": "started", "message": "ComfyUI model pull started. Poll /api/comfyui/pull/status for progress."}


@app.get("/api/comfyui/pull/status")
async def comfyui_pull_status():
    """Get ComfyUI pull progress."""
    with _state_lock:
        return dict(_comfyui_status)


class ComfyuiInstallNodeRequirementsRequest(BaseModel):
    node_path: str
    confirm: bool = False


@app.post("/api/comfyui/install-node-requirements")
async def comfyui_install_node_requirements_api(
    body: ComfyuiInstallNodeRequirementsRequest,
    request: Request,
):
    """Run pip install -r for a pack under ComfyUI custom_nodes (ops-controller → comfyui container)."""
    node = body.node_path.strip()
    if not node or ".." in node or node.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid node_path")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
    code, data = await _ops_request(
        "POST",
        "/comfyui/install-node-requirements",
        request=request,
        json={"node_path": node, "confirm": True},
        timeout=600.0,
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


class ModelDownloadRequest(BaseModel):
    url: str
    category: str = ""
    filename: str = ""


class ModelPullRequest(BaseModel):
    pack: str
    confirm: bool = False


def _normalize_gguf_pull_repos(model: str) -> str | None:
    """Return comma-separated Hugging Face repo ids for gguf-puller, or '' to use .env GGUF_MODELS.

    None means the string is not suitable (e.g. a bare tag like ``llama3.2:8b``).
    """
    def _normalize_repo_ref(raw: str) -> str | None:
        candidate = raw.strip()
        if not candidate:
            return None

        if "huggingface.co/" in candidate:
            match = re.search(r"huggingface\.co/([^/\s]+/[^/\s:#?]+)", candidate)
            if not match:
                return None
            candidate = match.group(1)
        elif candidate.startswith("hf.co/"):
            candidate = candidate[6:].strip()

        if ":" in candidate:
            repo, quant = candidate.rsplit(":", 1)
            if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and re.fullmatch(r"[\w.-]+", quant):
                return f"{repo}:{quant}"  # preserve quant filter for gguf-puller
            return None

        if re.fullmatch(r"[\w.-]+/[\w.-]+", candidate):
            return candidate
        return None

    s = (model or "").strip()
    if not s:
        return None
    if s.upper() in (".ENV", "GGUF_MODELS", "@ENV", "ENV"):
        return ""
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        normalized_parts: list[str] = []
        for p in parts:
            normalized = _normalize_repo_ref(p)
            if normalized is None:
                return None
            normalized_parts.append(normalized)
        return ",".join(normalized_parts)
    return _normalize_repo_ref(s)


def _hf_url_to_repo(raw: str) -> str:
    """Convert a HuggingFace GGUF URL to hf.co/owner/repo form for the gguf-puller.
    Non-HF strings (model names, hf.co/ refs) are returned as-is.
    """
    if "huggingface.co/" in raw:
        # https://huggingface.co/owner/repo/resolve/main/file.gguf → hf.co/owner/repo
        try:
            path = raw.split("huggingface.co/")[1].split("/resolve/")[0]
            return f"hf.co/{path}"
        except IndexError:
            pass
    return raw


@app.post("/api/models/download")
async def models_download(req: ModelDownloadRequest, request: Request):
    """Unified model download.
    - GGUF / HF repo → background gguf-puller via ops (same as ``/api/llm/pull``); poll ``/api/llm/pull/status``.
    - safetensors / ckpt / pt / bin → proxied to ops-controller for file download.
    """
    raw = req.url.strip()
    filename = req.filename.strip() or raw.split("/")[-1].split("?")[0]

    # Decide target from extension or URL pattern
    diffusion_exts = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")
    is_diffusion = any(filename.lower().endswith(e) for e in diffusion_exts)

    if is_diffusion:
        # Route to ops-controller (runs without uid 1000 restriction, has /models/comfyui mounted)
        if not raw.startswith("https://"):
            raise HTTPException(status_code=400, detail="URL must start with https://")
        code, data = await _ops_request(
            "POST", "/models/download", request=request,
            json={"url": raw, "category": req.category, "filename": req.filename},
        )
        if code >= 400:
            raise HTTPException(status_code=code, detail=data.get("detail", data))
        return {**data, "target": "comfyui"}
    else:
        with _state_lock:
            if _gguf_pull_status.get("running"):
                raise HTTPException(status_code=409, detail="Pull already in progress")
            _gguf_pull_status["running"] = True
        thread = threading.Thread(target=_run_gguf_pull, args=(raw,), daemon=True)
        thread.start()
        return {
            "status": "started",
            "target": "gguf",
            "message": "Poll /api/llm/pull/status for progress.",
        }


@app.get("/api/models/download/status")
async def models_download_status(request: Request):
    """Poll ComfyUI file download progress (proxied from ops-controller)."""
    code, data = await _ops_request("GET", "/models/download/status", request=request)
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.post("/api/models/pull")
async def models_pull(req: ModelPullRequest, request: Request):
    """Run comfyui-model-puller for a pack (e.g. flux1-dev). Works for gated models. Proxied to ops-controller."""
    code, data = await _ops_request(
        "POST", "/models/pull", request=request,
        json={"pack": req.pack.strip(), "confirm": req.confirm},
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return {**data, "target": "comfyui"}


@app.get("/api/models/pull/status")
async def models_pull_status(request: Request):
    """Poll pack pull progress (proxied from ops-controller)."""
    code, data = await _ops_request("GET", "/models/pull/status", request=request)
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


MCP_GATEWAY_SERVERS = os.environ.get("MCP_GATEWAY_SERVERS", "duckduckgo,n8n,searxng,comfyui,orchestration")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG_PATH")
# out/ordo.yaml, mounted RW (see dashboards/v1-parity/dashboard.yaml). servers.txt is render-owned, so
# persisting an MCP toggle means ALSO editing this source's `plugins:` list. Unset → servers.txt-only
# fallback (change is live but not persistent across a re-render).
ORDO_SOURCE_PATH = os.environ.get("ORDO_SOURCE_PATH")
# Suggested servers (dropdown). Users can also add any valid server name via custom input.
# `searxng` replaced `tavily` 2026-05-12 — search is now self-hosted via services.searxng.
MCP_CATALOG = [
    "duckduckgo", "n8n", "searxng", "comfyui", "orchestration", "fetch", "dockerhub", "github-official",
    "mongodb", "postgres", "stripe", "notion", "grafana", "elasticsearch",
    "documentation", "perplexity", "excalidraw", "miro", "neo4j",
    "time", "slack", "filesystem", "puppeteer", "context7", "memory",
    "firecrawl", "github", "git", "atlassian",
    "hugging-face",
]


def _mcp_config_path() -> Path | None:
    """Path to MCP servers config file (when dashboard has volume mounted)."""
    if not MCP_CONFIG_PATH:
        return None
    p = Path(MCP_CONFIG_PATH)
    return p if p.parent.exists() else None


def _normalize_server(s: str) -> str:
    """Parse URL to server ID, or return as-is if already valid."""
    parsed = _parse_mcp_server_input(s)
    return parsed if parsed else s


def _read_mcp_servers() -> list[str]:
    """Read enabled servers from config file or env. Normalizes URLs to server IDs and deduplicates."""
    path = _mcp_config_path()
    if path:
        if path.exists():
            raw = path.read_text().strip().replace("\r", "").replace("\n", ",")
            raw_list = [s.strip() for s in raw.split(",") if s.strip()]
            normalized = []
            seen = set()
            for s in raw_list:
                n = _normalize_server(s)
                if n and n not in seen:
                    normalized.append(n)
                    seen.add(n)
            # Persist cleanup if we changed anything (URLs → IDs)
            if normalized != raw_list:
                _write_mcp_servers(normalized)
            return normalized
        # Migrate: init file from .env on first run
        path.parent.mkdir(parents=True, exist_ok=True)
        initial = ",".join(s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()) or "duckduckgo,n8n,searxng,comfyui,orchestration"
        path.write_text(initial)
        return [s.strip() for s in initial.split(",") if s.strip()]
    return [s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()]


def _write_mcp_servers(servers: list[str]) -> Path:
    """Write servers to config file. Raises if not in dynamic mode."""
    path = _mcp_config_path()
    if not path:
        raise HTTPException(status_code=409, detail="MCP config not in dynamic mode (no volume)")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write-then-rename: survives file ownership mismatches on bind mounts
    # (the target file may be root-owned from an earlier write, but a world-writable
    # parent dir lets us create a new file and replace it regardless).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(",".join(servers))
    tmp.replace(path)
    return path


def _mcp_registry_path() -> Path | None:
    """Path to MCP registry.json (optional metadata)."""
    if not MCP_CONFIG_PATH:
        return None
    p = Path(MCP_CONFIG_PATH).parent / "registry.json"
    return p if p.parent.exists() else None


def _read_mcp_registry() -> dict:
    """Read registry.json if present. Falls back to empty dict."""
    path = _mcp_registry_path()
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("MCP registry read failed: %s", e)
    return {"servers": {}}


# ── Persistence: an MCP toggle must ALSO update ordo.yaml's `plugins:` list ────────────────────────
# servers.txt (what add/remove writes for the live gateway) is RENDER-OWNED — a re-render reseeds it
# from the enabled kind=mcp plugins in out/ordo.yaml. So a toggle that only touches servers.txt is
# ephemeral. To persist, we translate the toggled server_id → its plugin_id (via the render-emitted
# out/mcp/server-plugin-map.json) and surgically add/remove that `  - <plugin>` line in ordo.yaml,
# preserving every other line + comment. ordo.yaml stays the single source of truth: the next
# `ordo render` regenerates the SAME servers.txt → no drift.

# A `- <plugin-id>` item in a block-style YAML list (optional indent, dash, id, optional trailing
# comment). Zero-indent items are what `yaml.safe_dump` emits (a wizard-written source), so accept
# them too. MIRRORS ordo/source_edit.PLUGIN_ITEM_RE — keep the two in sync (the canonical is in the
# ordo package; the dashboard image doesn't vendor `ordo`, so this is a validated copy).
_PLUGIN_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s+(?P<id>[A-Za-z0-9._-]+)\s*(?:#.*)?$")


def _server_plugin_map_path() -> Path | None:
    """Path to the render-emitted server_id→plugin_id map (out/mcp/server-plugin-map.json), which
    sits alongside servers.txt in the mounted /mcp-config dir."""
    if not MCP_CONFIG_PATH:
        return None
    p = Path(MCP_CONFIG_PATH).parent / "server-plugin-map.json"
    return p if p.parent.exists() else None


def _read_server_plugin_map() -> dict[str, str]:
    """server_id → plugin_id for ALL registered kind=mcp plugins (enabled + available-but-disabled).
    Empty dict if the map isn't emitted/mounted (older render) — callers then treat every server as
    'not a known plugin' and fall back to servers.txt-only."""
    path = _server_plugin_map_path()
    if path and path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("MCP server-plugin-map read failed: %s", e)
    return {}


def _ordo_source_path() -> Path | None:
    """Path to the mounted, writable ordo.yaml source, or None (→ servers.txt-only fallback)."""
    if not ORDO_SOURCE_PATH:
        return None
    p = Path(ORDO_SOURCE_PATH)
    return p if p.exists() else None


def _edit_plugins_list(text: str, plugin_id: str, action: str) -> str:
    """Surgically add/remove `  - <plugin_id>` in ordo.yaml's block-style `plugins:` list, preserving
    every other line, comment, and the exact formatting. Pure text → text (no I/O), so it's unit-
    testable and the caller controls the write.

      action='remove': drop the matching item line(s). Returns text unchanged if already absent.
      action='add':    insert `  - <plugin_id>` (same indent/EOL as the last item) after the last
                       existing item. Returns text unchanged if already present.

    Raises ValueError if a safe edit can't be GUARANTEED — no block `plugins:` key, inline/flow list,
    empty list, or the result fails to round-trip through the YAML parser with exactly the intended
    change. The caller catches this, keeps the live servers.txt write, and surfaces a 'not persistent'
    note rather than risking the operator's hand-authored source.
    """
    if action not in ("add", "remove"):
        raise ValueError(f"unknown action {action!r}")
    lines = text.splitlines(keepends=True)
    # Locate a BARE `plugins:` block key (optional trailing comment only). An inline `plugins: [a, b]`
    # has content after the colon and is deliberately rejected — it can't be line-edited safely.
    key_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^plugins:\s*(?:#.*)?$", ln):
            key_idx = i
            break
    if key_idx is None:
        raise ValueError("ordo.yaml has no block-style `plugins:` list")
    # Collect the list items in this block; stop at the next top-level key. Blank lines and indented
    # comments are treated as still inside the block (they interleave the items).
    items: list[tuple[int, str]] = []   # (line index, plugin id)
    i = key_idx + 1
    while i < len(lines):
        ln = lines[i]
        m = _PLUGIN_ITEM_RE.match(ln)
        if m:
            items.append((i, m.group("id")))
            i += 1
        elif ln.strip() == "" or re.match(r"^\s+#", ln):
            i += 1
        elif re.match(r"^\S", ln):       # next top-level key — block ends
            break
        else:                            # unexpected indented, non-item content — stop, stay safe
            break
    if not items:
        raise ValueError("`plugins:` is empty or not a block-style list")

    present = [idx for idx, pid in items if pid == plugin_id]
    if action == "remove":
        if not present:
            return text
        drop = set(present)
        new_lines = [ln for j, ln in enumerate(lines) if j not in drop]
    else:  # add
        if present:
            return text
        last_idx = items[-1][0]
        m = _PLUGIN_ITEM_RE.match(lines[last_idx])
        indent = m.group("indent")
        eol = "\r\n" if lines[last_idx].endswith("\r\n") else "\n"
        new_line = f"{indent}- {plugin_id}{eol}"
        new_lines = lines[:last_idx + 1] + [new_line] + lines[last_idx + 1:]

    new_text = "".join(new_lines)
    # Safety net: the edit MUST round-trip and yield exactly the intended plugins-set change, or we
    # refuse it (raise) rather than persist a broken source.
    try:
        doc = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise ValueError(f"edited ordo.yaml no longer parses: {e}") from e
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, list):
        raise ValueError("edited ordo.yaml `plugins` is not a list")
    if action == "add" and plugin_id not in plugins:
        raise ValueError("plugin missing from `plugins` after add")
    if action == "remove" and plugin_id in plugins:
        raise ValueError("plugin still in `plugins` after remove")
    return new_text


def _persist_mcp_toggle(server: str, action: str) -> dict:
    """Persist an enable(action='add')/disable(action='remove') of MCP `server` into ordo.yaml's
    plugins list, in addition to the (already-done) live servers.txt write. Never raises — returns a
    status the endpoint attaches to its response:

      {persistent: bool, plugin: str|None, note: str|None}

    Not persistent (live-only) when: ordo.yaml isn't mounted/writable; the server isn't a registered
    mcp plugin (adding a brand-new non-plugin MCP to ordo.yaml is OUT OF SCOPE — flagged, not faked);
    or a safe surgical edit can't be guaranteed. In every such case servers.txt (the live path) still
    changed, so the toggle works now — it just won't survive a re-render, which the note states.
    """
    path = _ordo_source_path()
    if not path:
        return {"persistent": False, "plugin": None,
                "note": "ordo.yaml not mounted (ORDO_SOURCE_PATH unset) — change is live but will "
                        "not survive a re-render."}
    plugin = _read_server_plugin_map().get(server)
    if not plugin:
        return {"persistent": False, "plugin": None,
                "note": f"'{server}' is not a registered mcp plugin — updated the live servers.txt "
                        "only; it will not survive a re-render. Adding a brand-new non-plugin MCP to "
                        "ordo.yaml is out of scope."}
    try:
        original = path.read_text(encoding="utf-8")
        edited = _edit_plugins_list(original, plugin, action)
        if edited != original:
            # In-place write (NOT write-temp-then-rename): ordo.yaml is a SINGLE-FILE bind mount,
            # so the app user can neither create a sibling `.tmp` (its dir is the read-only container
            # root) nor rename over the mount. `edited` is already validated inside _edit_plugins_list
            # (round-trips through yaml.safe_load + asserts the exact plugins-set change), so writing
            # the known-good content directly is safe. (servers.txt keeps temp+rename because it lives
            # in a directory mount where a sibling tmp is writable.)
            path.write_text(edited, encoding="utf-8")
        return {"persistent": True, "plugin": plugin, "note": None}
    except (ValueError, OSError) as e:
        logger.warning("ordo.yaml persist failed for server=%s plugin=%s action=%s: %s",
                       server, plugin, action, e)
        return {"persistent": False, "plugin": plugin,
                "note": f"could not safely edit ordo.yaml ({e}) — change is live via servers.txt but "
                        "not persisted; ordo.yaml left untouched."}


MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "http://mcp-gateway:8811")


def _get_active_mcp_servers() -> list[str]:
    """Get enabled MCP servers from configuration file."""
    try:
        return _read_mcp_servers()
    except OSError:
        return []


def _mcp_catalog_from_registry() -> list[str]:
    """Build catalog from registry.json when present; otherwise use MCP_CATALOG."""
    reg = _read_mcp_registry()
    keys = list(reg.get("servers", {}).keys())
    if keys:
        return sorted(keys)
    return MCP_CATALOG.copy()


@app.get("/api/mcp/servers")
async def mcp_servers():
    """List enabled MCP servers (discovered from gateway) and catalog for adding."""
    active_servers = _get_active_mcp_servers()
    configured_servers = _read_mcp_servers()
    dynamic = _mcp_config_path() is not None
    registry = _read_mcp_registry()
    catalog = _mcp_catalog_from_registry()
    return {
        "enabled": active_servers,
        "configured": configured_servers,
        "catalog": catalog,
        "dynamic": dynamic,
        "registry": registry,
        "ok": True,
    }


@app.get("/api/mcp/health")
async def mcp_health():
    """MCP gateway health. Probes gateway; per-server status from ops-controller when available."""
    enabled = _read_mcp_servers()
    gateway_ok = False
    gateway_error = ""
    try:
        r = await _get_http_client().get(
            f"{MCP_GATEWAY_URL.rstrip('/')}/mcp",
            headers={"X-Client-ID": "dashboard"},
            timeout=5.0,
        )
        gateway_ok = r.status_code < 500
        if not gateway_ok:
            gateway_error = f"HTTP {r.status_code}"
    except Exception as e:
        gateway_error = str(e)

    # Per-server status: get from ops-controller (Docker) when token set
    container_status: dict[str, str] = {}
    if OPS_CONTROLLER_TOKEN:
        code, data = await _ops_request("GET", "/mcp/containers")
        if code == 200 and data.get("containers"):
            for c in data["containers"]:
                sid = c.get("id", "").split("/")[-1].split(":")[0] or c.get("name", "unknown")
                container_status[sid] = c.get("status", "unknown")

    servers = []
    for s in enabled:
        status = container_status.get(s, container_status.get(s.split("/")[-1]))
        ok = status == "running" if status else gateway_ok
        err = None if ok else (f"container: {status}" if status else gateway_error)
        servers.append({"id": s, "ok": ok, "error": err, "status": status or ("ok" if gateway_ok else "unreachable")})

    return {
        "ok": gateway_ok,
        "gateway": "reachable" if gateway_ok else "unreachable",
        "gateway_error": gateway_error if not gateway_ok else None,
        "servers": servers,
    }


class McpAddRequest(BaseModel):
    server: str


class McpRemoveRequest(BaseModel):
    server: str


def _valid_mcp_server_name(name: str) -> bool:
    """Allow alphanumeric, hyphens, underscores, slashes, colons (Docker refs)."""
    if not name or len(name) > 200:
        return False
    return all(c.isalnum() or c in "-_/:." for c in name)


def _parse_mcp_server_input(raw: str) -> str | None:
    """Extract server ID from input. Accepts:
    - Docker Hub MCP URL: https://hub.docker.com/mcp/server/hugging-face/overview -> hugging-face
    - Docker Hub image URL: https://hub.docker.com/r/searxng/searxng -> searxng/searxng
    - Raw server name: hugging-face, fetch, mcp/firecrawl
    """
    s = raw.strip()
    if not s:
        return None
    if "hub.docker.com" in s:
        # /mcp/server/<server-id>/...  (official MCP catalog page)
        idx = s.find("/mcp/server/")
        if idx >= 0:
            rest = s[idx + len("/mcp/server/"):].split("?", 1)[0].split("#", 1)[0]
            server_id = rest.split("/", 1)[0]
            if server_id and _valid_mcp_server_name(server_id):
                return server_id
        # /r/<org>/<image>/...  (generic Docker Hub image page)
        idx = s.find("/r/")
        if idx >= 0:
            rest = s[idx + len("/r/"):].split("?", 1)[0].split("#", 1)[0].rstrip("/")
            parts = rest.split("/")
            if len(parts) >= 2 and parts[0] and parts[1]:
                image_ref = f"{parts[0]}/{parts[1]}"
                if _valid_mcp_server_name(image_ref):
                    return image_ref
    return s if _valid_mcp_server_name(s) else None


@app.post("/api/mcp/add")
async def mcp_add(req: McpAddRequest):
    """Add an MCP server. Takes effect in ~10s without container restart.
    Accepts: server name (fetch, hugging-face), Docker ref (mcp/firecrawl),
    or Docker Hub URL (https://hub.docker.com/mcp/server/hugging-face/overview)."""
    server = _parse_mcp_server_input(req.server)
    if not server:
        raise HTTPException(status_code=400, detail="Invalid server name or URL. Use a name (e.g. hugging-face) or paste a Docker Hub MCP URL.")
    servers = _read_mcp_servers()
    if server in servers:
        return {"status": "already_enabled", "servers": servers}
    servers.append(server)
    _write_mcp_servers(servers)                       # live: gateway hot-reloads on its poll
    persist = _persist_mcp_toggle(server, "add")      # persist: add the plugin to ordo.yaml's list
    logger.info("MCP_SERVER_ADDED server=%s persistent=%s plugin=%s",
                server, persist["persistent"], persist["plugin"])
    return {"status": "added", "servers": servers, **persist}


@app.post("/api/mcp/remove")
async def mcp_remove(req: McpRemoveRequest):
    """Remove an MCP server. Takes effect in ~10s without container restart."""
    server = _parse_mcp_server_input(req.server) or req.server.strip()
    if not server:
        raise HTTPException(status_code=400, detail="Server name required")
    servers = _read_mcp_servers()
    if server not in servers:
        return {"status": "already_removed", "servers": servers}
    servers = [s for s in servers if s != server]
    if not servers:
        raise HTTPException(status_code=400, detail="Cannot remove last server. Add another first.")
    _write_mcp_servers(servers)                        # live: gateway hot-reloads on its poll
    persist = _persist_mcp_toggle(server, "remove")    # persist: drop the plugin from ordo.yaml's list
    logger.info("MCP_SERVER_REMOVED server=%s persistent=%s plugin=%s",
                server, persist["persistent"], persist["plugin"])
    return {"status": "removed", "servers": servers, **persist}


# --- Token Throughput ---

_throughput_samples: dict[str, list[dict]] = {}   # {"tps": float, "ts": epoch}
_ttft_samples: dict[str, list[dict]] = {}         # {"ms": float, "ts": epoch}
_MAX_SAMPLES_PER_MODEL = 500
_MAX_TRACKED_MODELS = 50
# v2: samples are timestamped dicts. v1 stored bare floats keyed largely by routing
# ALIAS (one `local-chat` bucket conflating every model ever active behind it, CPU
# failover included) — unattributable, so version bumps trigger a clean reset.
_THROUGHPUT_STORE_VERSION = 2
_SAMPLE_MAX_AGE_SEC = 7 * 86400  # models with no sample in 7 days leave the store


def _evict_stale_models(now: float) -> None:
    """Drop models whose newest sample is older than _SAMPLE_MAX_AGE_SEC.
    Call while holding _state_lock."""
    cutoff = now - _SAMPLE_MAX_AGE_SEC
    for store in (_throughput_samples, _ttft_samples):
        stale = [m for m, s in store.items() if not s or s[-1]["ts"] < cutoff]
        for m in stale:
            del store[m]

# Last benchmark result (persists across page refresh until dashboard restart)
_last_benchmark: dict | None = None

# Service usage: list of { model, service, tps, ts } for "which service uses which model"
_service_usage: list[dict] = []
_MAX_SERVICE_USAGE = 500

DASHBOARD_DATA_PATH = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
DASHBOARD_DATA_PATH.mkdir(parents=True, exist_ok=True)
_THROUGHPUT_FILE = DASHBOARD_DATA_PATH / "throughput.json"


def _load_throughput_state() -> None:
    """Load throughput samples and last benchmark from disk (R4). v1 files (no
    version field) get a clean reset — their samples are un-timestamped and
    alias-conflated; only last_benchmark carries over."""
    global _throughput_samples, _ttft_samples, _last_benchmark, _service_usage
    if not _THROUGHPUT_FILE.exists():
        return
    try:
        data = json.loads(_THROUGHPUT_FILE.read_text(encoding="utf-8"))
        _last_benchmark = data.get("last_benchmark") if isinstance(data.get("last_benchmark"), dict) else None
        if data.get("version") != _THROUGHPUT_STORE_VERSION:
            logger.warning(
                "Throughput store is v%s (want v%s) — resetting samples, keeping last_benchmark",
                data.get("version", 1), _THROUGHPUT_STORE_VERSION,
            )
            return
        _throughput_samples = {
            k: [s for s in v if isinstance(s, dict) and "tps" in s and "ts" in s]
            for k, v in (data.get("samples") or {}).items() if isinstance(v, list)
        }
        _ttft_samples = {
            k: [s for s in v if isinstance(s, dict) and "ms" in s and "ts" in s]
            for k, v in (data.get("ttft_samples") or {}).items() if isinstance(v, list)
        }
        _service_usage = [u for u in (data.get("service_usage") or []) if isinstance(u, dict)][-_MAX_SERVICE_USAGE:]
    except Exception as e:
        logger.warning("Throughput state load failed: %s", e)


def _save_throughput_state() -> None:
    """Persist throughput state to disk via atomic write-then-rename."""
    try:
        _THROUGHPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _THROUGHPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "version": _THROUGHPUT_STORE_VERSION,
            "samples": _throughput_samples,
            "ttft_samples": _ttft_samples,
            "last_benchmark": _last_benchmark,
            "service_usage": _service_usage[-_MAX_SERVICE_USAGE:],
        }), encoding="utf-8")
        tmp.replace(_THROUGHPUT_FILE)
    except Exception as e:
        logger.warning("Throughput state save failed: %s", e)


_throughput_last_save: float = 0.0
_THROUGHPUT_SAVE_INTERVAL: float = 5.0


def _maybe_save_throughput() -> None:
    """Debounced save: write at most every _THROUGHPUT_SAVE_INTERVAL seconds."""
    global _throughput_last_save
    now = time.monotonic()
    if now - _throughput_last_save >= _THROUGHPUT_SAVE_INTERVAL:
        _save_throughput_state()
        _throughput_last_save = now


_load_throughput_state()


def _percentile(sorted_arr: list[float], p: float) -> float:
    """Compute percentile (0–100). Returns 0 if empty."""
    if not sorted_arr:
        return 0.0
    k = (len(sorted_arr) - 1) * (p / 100)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_arr) else f
    return sorted_arr[f] + (k - f) * (sorted_arr[c] - sorted_arr[f]) if c > f else sorted_arr[f]


class ThroughputBenchmarkRequest(BaseModel):
    model: str = ""


class ThroughputRecordRequest(BaseModel):
    model: str = Field(default="", max_length=256)
    output_tokens_per_sec: float = Field(default=0.0, ge=0, le=1e6)
    service: str = Field(default="", max_length=64)
    ttft_ms: float = Field(default=0.0, ge=0, le=1e6)
    alias: str = Field(default="", max_length=256)
    backend: str = Field(default="", max_length=64)


@app.post("/api/throughput/record")
async def throughput_record(req: ThroughputRecordRequest):
    """Record a throughput sample from real-world usage (e.g. model gateway). Fire-and-forget."""
    model = req.model.strip()
    if not model or req.output_tokens_per_sec <= 0:
        return {"ok": True}
    now = time.time()
    with _state_lock:
        _evict_stale_models(now)
        if model not in _throughput_samples:
            if len(_throughput_samples) >= _MAX_TRACKED_MODELS:
                return {"ok": True}
            _throughput_samples[model] = []
        _throughput_samples[model].append({"tps": req.output_tokens_per_sec, "ts": now})
        if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
            _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        if req.ttft_ms > 0 and (model in _ttft_samples or len(_ttft_samples) < _MAX_TRACKED_MODELS):
            if model not in _ttft_samples:
                _ttft_samples[model] = []
            _ttft_samples[model].append({"ms": req.ttft_ms, "ts": now})
            if len(_ttft_samples[model]) > _MAX_SAMPLES_PER_MODEL:
                _ttft_samples[model] = _ttft_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        # Service usage (which service is taxing which model)
        service = (req.service or "unknown").strip()[:64]
        _service_usage.append({
            "model": model,
            "service": service,
            "alias": req.alias.strip()[:256],
            "backend": req.backend.strip()[:64],
            "tps": round(req.output_tokens_per_sec, 1),
            "ttft_ms": round(req.ttft_ms, 1) if req.ttft_ms > 0 else 0.0,
            "ts": now,
        })
        if len(_service_usage) > _MAX_SERVICE_USAGE:
            _service_usage[:] = _service_usage[-_MAX_SERVICE_USAGE:]
        _maybe_save_throughput()
    return {"ok": True}


@app.get("/api/throughput/service-usage")
async def throughput_service_usage():
    """Return recent service usage: which service used which model (from model gateway traffic)."""
    now = time.time()
    with _state_lock:
        usage_snapshot = list(_service_usage)
    recent = [u for u in usage_snapshot if (now - u["ts"]) < 86400]
    by_model: dict[str, list[dict]] = {}
    for u in recent:
        m = u["model"]
        if m not in by_model:
            by_model[m] = []
        by_model[m].append({
            "service": u["service"],
            "tps": u["tps"],
            "ts": u["ts"],
        })
    # Per model: unique services, last activity, last tps per service
    result: dict[str, dict] = {}
    for model, usages in by_model.items():
        by_svc: dict[str, list] = {}
        for u in usages:
            s = u["service"]
            if s not in by_svc:
                by_svc[s] = []
            by_svc[s].append({"tps": u["tps"], "ts": u["ts"], "ttft_ms": u.get("ttft_ms", 0.0)})
        result[model] = {
            "services": [
                {
                    "name": svc,
                    "last_tps": max(u["tps"] for u in vals),
                    "last_ttft_ms": max(u.get("ttft_ms", 0.0) for u in vals),
                    "last_ts": max(u["ts"] for u in vals),
                    "count": len(vals),
                }
                for svc, vals in by_svc.items()
            ],
        }
    return {"by_model": result, "ok": True}


# Authoritative active model, from the same ops-controller /model-config the Model
# Control tab uses. Cached (positive AND negative) so a 10s-poll dashboard doesn't
# hammer ops; null means "unknown" and the UI says so instead of guessing.
_ACTIVE_MODEL_CACHE_TTL = 30.0
_active_model_cache: dict = {"checked": 0.0, "value": None}


async def _throughput_active_model() -> str | None:
    now = time.monotonic()
    if now - _active_model_cache["checked"] < _ACTIVE_MODEL_CACHE_TTL:
        return _active_model_cache["value"]
    code, data = await _ops_request("GET", "/model-config", timeout=10.0)
    value = None
    if code == 200 and isinstance(data, dict) and data.get("active_model"):
        value = str(data["active_model"])
    else:
        logger.warning("throughput active-model fetch failed (HTTP %s)", code)
    _active_model_cache["checked"] = now
    _active_model_cache["value"] = value
    return value


@app.get("/api/throughput/stats")
async def throughput_stats():
    """Per-model throughput stats over timestamped samples: peak, p50/p95/p99, latest,
    sample_count, first_ts/last_ts. Includes last_benchmark if available."""
    result: dict[str, dict] = {}
    now = time.time()
    with _state_lock:
        _evict_stale_models(now)
        snapshot = {m: list(s) for m, s in _throughput_samples.items()}
        ttft_snapshot = {m: list(s) for m, s in _ttft_samples.items()}
        benchmark = dict(_last_benchmark) if _last_benchmark else None
    for model, samples in snapshot.items():
        if not samples:
            continue
        tps_vals = [s["tps"] for s in samples]
        sorted_s = sorted(tps_vals)
        ttfts = [s["ms"] for s in ttft_snapshot.get(model, [])]
        sorted_ttfts = sorted(ttfts)
        result[model] = {
            "latest": round(tps_vals[-1], 1),
            "peak": round(max(tps_vals), 1),
            "p50": round(_percentile(sorted_s, 50), 1),
            "p95": round(_percentile(sorted_s, 95), 1),
            "p99": round(_percentile(sorted_s, 99), 1),
            "ttft_p50_ms": round(_percentile(sorted_ttfts, 50), 1) if sorted_ttfts else 0.0,
            "ttft_p95_ms": round(_percentile(sorted_ttfts, 95), 1) if sorted_ttfts else 0.0,
            "sample_count": len(samples),
            "first_ts": samples[0]["ts"],
            "last_ts": samples[-1]["ts"],
        }
    out: dict = {"models": result, "ok": True, "active_model": await _throughput_active_model()}
    if benchmark:
        out["last_benchmark"] = benchmark
    return out


@app.get("/api/performance/summary")
async def performance_summary():
    """Compact performance summary for dashboards, automation, and audits."""
    with _state_lock:
        snapshot = {m: list(s) for m, s in _throughput_samples.items()}
        ttft_snapshot = {m: list(s) for m, s in _ttft_samples.items()}
        benchmark = dict(_last_benchmark) if _last_benchmark else None
        recent_usage = list(_service_usage)
    now = time.time()
    recent_usage = [u for u in recent_usage if (now - u["ts"]) < 86400]
    top_models = []
    for model, samples in snapshot.items():
        if not samples:
            continue
        tps_vals = [s["tps"] for s in samples]
        sorted_s = sorted(tps_vals)
        ttfts = [s["ms"] for s in ttft_snapshot.get(model, [])]
        sorted_ttfts = sorted(ttfts)
        top_models.append(
            {
                "model": model,
                "latest_tps": round(tps_vals[-1], 1),
                "p95_tps": round(_percentile(sorted_s, 95), 1),
                "latest_ttft_ms": round(ttfts[-1], 1) if ttfts else 0.0,
                "p95_ttft_ms": round(_percentile(sorted_ttfts, 95), 1) if sorted_ttfts else 0.0,
                "sample_count": len(samples),
                "last_ts": samples[-1]["ts"],
            }
        )
    top_models.sort(key=lambda item: item["last_ts"], reverse=True)
    try:
        rag = await asyncio.wait_for(rag_status(), timeout=2.0)
    except TimeoutError:
        rag = {"ok": False, "error": "timeout"}
    return {
        "ok": True,
        "llamacpp_ctx_size": int(os.environ.get("LLAMACPP_CTX_SIZE", "262144") or 262144),
        "worker_concurrency": int(os.environ.get("WORKER_CONCURRENCY", "1") or 1),
        "throughput": {
            "tracked_models": len(top_models),
            "top_models": top_models[:10],
            "last_benchmark": benchmark,
            "service_events_24h": len(recent_usage),
        },
        "orchestration": {
            "jobs": get_job_counts(DASHBOARD_DATA_PATH),
            "outbox": get_outbox_stats(DASHBOARD_DATA_PATH),
        },
        "rag": rag,
    }


@app.get("/api/llm/ps")
async def llm_ps():
    """List models currently advertised by model-gateway."""
    try:
        r = await _get_http_client().get(
            f"{MODEL_GATEWAY_URL.rstrip('/')}/v1/models",
            headers=_model_gateway_headers(),
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        models = [{"name": m["id"]} for m in data.get("data", []) if m.get("id")]
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Model gateway request failed: {e}")


# Embedding models don't support chat completions — exclude from throughput benchmark
_EMBED_MODEL_PATTERNS = ("embed", "bge", "mxbai", "arctic-embed", "granite-embedding", "paraphrase-multilingual")


def _is_embedding_model(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _EMBED_MODEL_PATTERNS)


@app.post("/api/throughput/benchmark")
async def throughput_benchmark(req: ThroughputBenchmarkRequest):
    """Run a quick benchmark via model-gateway /v1/chat/completions."""
    model = req.model.strip() or "local-chat"
    if _is_embedding_model(model):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is an embedding model and does not support text generation. Choose an LLM (e.g. local-chat).",
        )
    prompt = "Say 'ok' and nothing else."
    url = f"{MODEL_GATEWAY_URL.rstrip('/')}/v1/chat/completions"
    try:
        started = time.perf_counter()
        r = await _get_http_client().post(
            url,
            headers=_model_gateway_headers(),
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16,
                "stream": False,
            },
            timeout=60.0,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if r.status_code == 400:
            try:
                err = r.json()
                error_obj = err.get("error", err)
                if isinstance(error_obj, dict):
                    msg = error_obj.get("message") or error_obj.get("error") or r.text or "Bad request"
                else:
                    msg = str(error_obj) or r.text or "Bad request"
            except (ValueError, UnicodeDecodeError, KeyError):
                msg = r.text or "Bad request"
            raise HTTPException(status_code=400, detail=f"Model gateway: {msg}")
        r.raise_for_status()
        data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Model gateway request failed: {e}")

    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    eval_count = int(usage.get("completion_tokens") or 0)
    prompt_eval_count = int(usage.get("prompt_tokens") or 0)
    elapsed_sec = max(elapsed_ms / 1000, 0.001)

    # Prefer server-reported eval speed when available (avoids network overhead inflation)
    timings = data.get("timings", {}) if isinstance(data, dict) else {}
    if isinstance(timings, dict) and timings.get("predicted_per_second"):
        output_tokens_per_sec = float(timings["predicted_per_second"])
    else:
        output_tokens_per_sec = eval_count / elapsed_sec if eval_count > 0 else 0
    input_tokens_per_sec = prompt_eval_count / elapsed_sec if prompt_eval_count > 0 else 0

    payload = {
        "ok": True,
        "model": model,
        "prompt_tokens": prompt_eval_count,
        "output_tokens": eval_count,
        "output_tokens_per_sec": round(output_tokens_per_sec, 1),
        "input_tokens_per_sec": round(input_tokens_per_sec, 1),
        "eval_duration_ms": round(elapsed_ms, 1),
        "load_duration_ms": 0.0,
        "total_duration_ms": round(elapsed_ms, 1),
    }
    global _last_benchmark
    with _state_lock:
        _last_benchmark = payload
        _save_throughput_state()
    return payload


# --- Ops Controller proxy ---

OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "")


async def _ops_request(
    method: str,
    path: str,
    request: Request | None = None,
    *,
    timeout: float = 30.0,
    **kwargs,
) -> tuple[int, dict]:
    """Proxy request to ops controller. Returns (status_code, json_body).
    Forwards X-Request-ID when present for audit correlation.
    """
    if not OPS_CONTROLLER_TOKEN:
        return 503, {"detail": "OPS_CONTROLLER_TOKEN not configured"}
    url = f"{OPS_CONTROLLER_URL.rstrip('/')}{path}"
    extra = kwargs.pop("headers", {})
    if request and request.headers.get("X-Request-ID"):
        extra = {**extra, "X-Request-ID": request.headers["X-Request-ID"]}
    headers = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}", **extra}
    try:
        r = await _get_http_client().request(method, url, headers=headers, timeout=timeout, **kwargs)
        try:
            data = r.json()
        except (ValueError, UnicodeDecodeError):
            data = {"detail": r.text or "Unknown error"}
        return r.status_code, data
    except Exception as e:
        return 503, {"detail": str(e)}


@app.post("/api/ops/services/{service_id}/start")
async def ops_start(service_id: str, request: Request):
    """Start a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request(
        "POST", f"/services/{ops_id}/start", request=request, json={"confirm": True}
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.post("/api/ops/services/{service_id}/stop")
async def ops_stop(service_id: str, request: Request):
    """Stop a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request(
        "POST", f"/services/{ops_id}/stop", request=request, json={"confirm": True}
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.post("/api/ops/services/{service_id}/restart")
async def ops_restart(service_id: str, request: Request):
    """Restart a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request(
        "POST", f"/services/{ops_id}/restart", request=request, json={"confirm": True}
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.get("/api/ops/services/{service_id}/logs")
async def ops_logs(service_id: str, request: Request, tail: int = 100):
    """Get service logs via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request(
        "GET", f"/services/{ops_id}/logs?tail={tail}", request=request
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.get("/api/ops/available")
async def ops_available(request: Request):
    """Check if ops controller is configured and reachable."""
    if not OPS_CONTROLLER_TOKEN:
        return {"available": False, "reason": "OPS_CONTROLLER_TOKEN not set"}
    code, _ = await _ops_request("GET", "/health", request=request)
    return {"available": code == 200}


# --- Default model ---

class DefaultModelRequest(BaseModel):
    model: str


@app.get("/api/config/default-model")
async def get_default_model(request: Request):
    """Return DEFAULT_MODEL plus the Open WebUI-specific default from project .env when configured."""
    if OPS_CONTROLLER_TOKEN:
        code, data = await _ops_request("GET", "/env/DEFAULT_MODEL", request=request)
        if code == 200 and isinstance(data, dict):
            code2, data2 = await _ops_request("GET", "/env/OPEN_WEBUI_DEFAULT_MODEL", request=request)
            return {
                "default_model": (data.get("value") or "").strip(),
                "open_webui_default_model": (data2.get("value") or "").strip()
                if code2 == 200 and isinstance(data2, dict)
                else "",
            }
    return {
        "default_model": os.environ.get("DEFAULT_MODEL", ""),
        "open_webui_default_model": os.environ.get("OPEN_WEBUI_DEFAULT_MODEL", ""),
    }


def _open_webui_default_model(name: str) -> str:
    model = (name or "").strip()
    if not model:
        return ""
    lower = model.lower()
    if model.endswith(":chat") or "embed" in lower:
        return model
    return f"{model}:chat"


@app.post("/api/config/default-model")
async def set_default_model(req: DefaultModelRequest, request: Request):
    """Write DEFAULT_MODEL and OPEN_WEBUI_DEFAULT_MODEL to .env and recreate open-webui."""
    # Model ids may be namespaced: owner/model:tag (slashes allowed). Only reject empty / traversal.
    name = (req.model or "").strip()
    if not name or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid model name")
    open_webui_model = _open_webui_default_model(name)

    # 1. Write to .env
    code, data = await _ops_request(
        "POST", "/env/set", request=request,
        json={"key": "DEFAULT_MODEL", "value": name, "confirm": True},
    )
    if code >= 400:
        raise HTTPException(status_code=502, detail=f"env/set failed: {data.get('detail', data)}")
    code_ui, data_ui = await _ops_request(
        "POST", "/env/set", request=request,
        json={"key": "OPEN_WEBUI_DEFAULT_MODEL", "value": open_webui_model, "confirm": True},
    )
    if code_ui >= 400:
        raise HTTPException(status_code=502, detail=f"env/set failed: {data_ui.get('detail', data_ui)}")

    # 2. Recreate open-webui so DEFAULT_MODELS env var is picked up
    code2, data2 = await _ops_request(
        "POST", "/services/open-webui/recreate", request=request, json={"confirm": True}
    )

    return {
        "ok": code2 in (200, 201),
        "model": name,
        "open_webui_model": open_webui_model,
        "webui_recreated": code2 in (200, 201),
        "webui_error": data2.get("detail") if code2 >= 400 else None,
    }


# --- RAG ---

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
RAG_COLLECTION = os.environ.get("RAG_COLLECTION", "documents")


@app.get("/api/rag/status")
async def rag_status():
    """Qdrant health and document collection stats. No auth required."""
    try:
        r = await _get_http_client().get(f"{QDRANT_URL}/collections/{RAG_COLLECTION}", timeout=5.0)
        if r.status_code == 200:
            info = r.json().get("result", {})
            return {
                "ok": True,
                "collection": RAG_COLLECTION,
                "points_count": info.get("points_count", 0),
                "status": info.get("status", "unknown"),
            }
        if r.status_code == 404:
            return {"ok": True, "collection": RAG_COLLECTION, "points_count": 0, "status": "empty"}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Hardware ---

# Disk usage probe path. Defaults to a bind-mount (NOT '/') so psutil sees the
# host volume's real free/used instead of the small Docker overlay layer
# (which is what shows up at `/` inside the container). Every dashboard bind
# mount points back at the same host C:/ drive, so any of them reports
# correct host-disk stats — `/data/dashboard` is always mounted, smallest, and
# semantically the right place to ask "how much room do I have for state?".
# Operator can still override via BASE_PATH env if they want a different
# mount (e.g. a separate drive for models).
BASE_PATH_ENV = os.environ.get("BASE_PATH", "/data/dashboard")


def _nvml_vram_to_gpu_dict(
    name: str,
    used_b: int,
    total_b: int,
    util_pct: int,
) -> dict | None:
    """Build gpu payload with decimal GB only (UI shows these strings — no client-side byte math)."""
    total_b = int(total_b)
    if total_b <= 0:
        return None
    used_b = max(0, int(used_b))
    if used_b > total_b:
        used_b = total_b
    return {
        "name": name or "GPU",
        "vram_used_gb": round(used_b / 1e9, 1),
        "vram_total_gb": round(total_b / 1e9, 1),
        "utilization_pct": int(util_pct),
    }


def _probe_gpu() -> dict | None:
    """Best-effort GPU stats with multi-source fallback.

    NVML (pynvml) is the preferred path BUT on Windows Docker Desktop with
    recent CUDA drivers, `nvmlDeviceGetMemoryInfo` returns garbage for free /
    used (memory.free reports ~4.4 TB, memory.used overflows to ~1.8e19 GB).
    We detect that and fall back to `nvidia-smi --query-gpu=memory.free,memory.total --format=csv`
    which has internal sanity-checking and returns correct values.
    """
    name = "GPU"
    util_pct = 0
    total_b: int | None = None
    used_b: int | None = None
    source = "nvml"

    # Layer 1: NVML
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            mi = pynvml.nvmlDeviceGetMemoryInfo(h)
            ut = pynvml.nvmlDeviceGetUtilizationRates(h)
            nm = pynvml.nvmlDeviceGetName(h)
            if isinstance(nm, bytes):
                name = nm.decode("utf-8", errors="replace").strip()
            else:
                name = str(nm).strip()
            util_pct = int(ut.gpu)
            t = int(mi.total)
            f = int(mi.free)
            u = int(mi.used)
            if t > 0:
                total_b = t
            # Sanity-check NVML's memory fields. On this driver they wrap to
            # values > total — discard those.
            if total_b is not None and 0 <= u <= total_b:
                used_b = u
            elif total_b is not None and 0 <= f <= total_b:
                used_b = total_b - f
            # else: leave used_b unset; fall through to nvidia-smi
        finally:
            pynvml.nvmlShutdown()
    except Exception as e:
        logger.debug("NVML probe failed: %s", e)

    # Layer 2: nvidia-smi shell fallback
    if total_b is None or used_b is None:
        try:
            import subprocess
            cmd = [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ]
            out = subprocess.check_output(cmd, text=True, timeout=4).strip().splitlines()[0]
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 5:
                if name == "GPU" and parts[0]:
                    name = parts[0]
                t = int(float(parts[1])) * 1024 * 1024     # MiB -> bytes
                f = int(float(parts[2])) * 1024 * 1024
                u_raw = int(float(parts[3])) * 1024 * 1024
                util_pct = int(float(parts[4])) if parts[4] else util_pct
                total_b = t
                # nvidia-smi memory.used can wrap; prefer total-free unless
                # used looks sensible.
                if 0 <= u_raw <= t:
                    used_b = u_raw
                elif 0 <= f <= t:
                    used_b = t - f
                source = "nvidia-smi"
        except Exception as e:
            logger.debug("nvidia-smi probe failed: %s", e)

    if total_b is None:
        return None
    if used_b is None:
        # Memory reading came back garbage from BOTH sources (NVML wrap + nvidia-smi
        # under heavy load). Return total + util but flag the used field as unknown
        # so the UI can render "N/A" instead of confidently misleading "100%".
        return {
            "name": name or "GPU",
            "vram_used_gb": None,
            "vram_total_gb": round(total_b / 1e9, 1),
            "utilization_pct": int(util_pct),
            "memory_reading_reliable": False,
            "source": source,
        }
    gpu = _nvml_vram_to_gpu_dict(name, used_b, total_b, util_pct)
    if gpu is not None:
        gpu["source"] = source
        gpu["memory_reading_reliable"] = True
    return gpu


@app.get("/api/hardware")
async def hardware_stats():
    """System resource stats. No auth required (read-only). Blocking calls run in thread pool (R7)."""
    cpu_pct = await asyncio.to_thread(psutil.cpu_percent, 0.1)
    mem = await asyncio.to_thread(psutil.virtual_memory)
    try:
        disk = await asyncio.to_thread(psutil.disk_usage, BASE_PATH_ENV)
        disk_used_gb = round(disk.used / 1e9, 1)
        disk_total_gb = round(disk.total / 1e9, 1)
        disk_pct = round(disk.percent, 1) if disk.total > 0 else 0
    except Exception as e:
        logger.warning("Disk usage check failed for %s: %s", BASE_PATH_ENV, e)
        disk_used_gb = None
        disk_total_gb = None
        disk_pct = None

    gpu = await asyncio.to_thread(_probe_gpu)
    try:
        gpus = (await asyncio.to_thread(gpu_stats.list_gpus)).get("gpus", [])
    except Exception as e:
        logger.debug("multi-GPU enumeration failed: %s", e)
        gpus = []

    return {
        "cpu_pct": cpu_pct,
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_pct": mem.percent,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
        "gpu": gpu,
        "gpus": gpus,
    }

@app.get("/api/hardware/service-pressure")
async def service_pressure():
    """Per-service compute pressure (CPU/RAM/VRAM). No auth — read-only, like /api/hardware."""
    from dashboard.services_catalog import OPS_SERVICE_MAP, SERVICES

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    host_info = {
        "cpu_cores": psutil.cpu_count() or 0,
        "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
    }

    def _empty_payload():
        services_out = [{
            "id": s["id"], "name": s["name"],
            "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
            "vram_gb": 0.0, "vram_pct": 0.0,
            "has_gpu": bool(s.get("has_gpu", False)),
            "running": False,
        } for s in SERVICES]
        return {"gpu": None, "host": host_info, "services": services_out, "vram_aggregate_unavailable": True}

    try:
        async with _httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{ops_url}/stats/services", headers=headers)
            if r.status_code != 200:
                return _empty_payload()
            raw = r.json()
    except (_httpx.RequestError, OSError) as e:
        logger.debug("service-pressure: ops-controller unreachable: %s", e)
        return _empty_payload()

    raw_services: dict = raw.get("services") or {}
    catalog = {s["id"]: s for s in SERVICES}
    compose_to_display = {v: k for k, v in OPS_SERVICE_MAP.items()}

    services_out: list[dict] = []
    for compose_id, row in raw_services.items():
        display_id = compose_to_display.get(compose_id, compose_id)
        cat = catalog.get(display_id)
        services_out.append({
            "id": display_id,
            "name": (cat or {}).get("name") or compose_id,
            "cpu_pct": float(row.get("cpu_pct") or 0.0),
            "mem_gb": float(row.get("mem_gb") or 0.0),
            "mem_pct": float(row.get("mem_pct") or 0.0),
            "vram_gb": float(row.get("vram_gb") or 0.0),
            "vram_pct": float(row.get("vram_pct") or 0.0),
            "has_gpu": bool((cat or {}).get("has_gpu", False)),
            "running": bool(row.get("running", False)),
        })
    seen = {s["id"] for s in services_out}
    for cid, cat in catalog.items():
        if cid not in seen:
            services_out.append({
                "id": cid, "name": cat["name"],
                "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
                "vram_gb": 0.0, "vram_pct": 0.0,
                "has_gpu": bool(cat.get("has_gpu", False)),
                "running": False,
            })
    services_out.sort(
        key=lambda s: max(s["cpu_pct"], s["mem_pct"], s["vram_pct"]),
        reverse=True,
    )
    return {
        "gpu": raw.get("gpu"),
        "host": host_info,
        "services": services_out,
        "vram_aggregate_unavailable": bool(raw.get("vram_aggregate_unavailable", False)),
    }


# --- GPU routes ---

_routes_gpu.register(app, _ops_request)
_routes_registry.register(app, _ops_request)
_routes_model_config.register(app, _ops_request)

# --- Static ---


class _NoCacheHTMLStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation of the HTML app shell.

    Starlette's StaticFiles sends an ETag + Last-Modified but NO Cache-Control,
    which lets browsers apply *heuristic* freshness and serve a stale index.html
    without revalidating — so a rebuilt dashboard (new SSO routes, new service
    cards, etc.) can keep showing the old shell until a hard refresh. We add
    `Cache-Control: no-cache` to HTML responses only: the browser still caches
    the shell but MUST revalidate the ETag every load, so a new build is picked
    up immediately.

    Vite emits every JS/CSS chunk under ``assets/`` with a content hash in the
    filename (``index-a1b2c3d4.js``), so a changed file gets a NEW URL — the old
    URL can safely be cached forever. We mark those responses
    ``public, max-age=31536000, immutable`` so browsers never revalidate them,
    eliminating a conditional request per asset per load. Non-hashed files at the
    SPA root (favicon, manifest, etc.) are left with StaticFiles' default (ETag /
    Last-Modified revalidation) so they can't go stale.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        elif response.status_code == 200 and path.replace("\\", "/").startswith("assets/"):
            # Content-hashed Vite assets — immutable, cache for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


static_dir = Path(__file__).parent / "static"
frontend_dist = Path(__file__).parent / "frontend" / "dist"

# The dashboard SPA is the React build in frontend/dist when present (production image
# and any local `npm run build`), otherwise the legacy vanilla shell in static/. The
# legacy shell is always preserved and reachable at /legacy-index.html (a fallback while
# the React port is validated). A production Vite build emits hashed ES modules referenced
# with script-src 'self', so it satisfies the app's strict CSP. All /api/* and /grafana/*
# routes are registered above and take precedence over these catch-all static mounts.
_spa_dir = frontend_dist if (frontend_dist / "index.html").exists() else static_dir


@app.get("/legacy-index.html", include_in_schema=False)
async def legacy_shell():
    """Serve the preserved legacy vanilla-JS dashboard shell."""
    legacy = static_dir / "legacy-index.html"
    if not legacy.exists():
        raise HTTPException(status_code=404, detail="legacy shell not present")
    return FileResponse(str(legacy), media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def _app_shell():
    """Serve the SPA app shell with revalidation headers. Uses the React build's index when
    present (production image / local `npm run build`), else the preserved legacy shell — so
    `/` still returns a 200 shell in a headless env (CI/tests) where the React build hasn't
    run and `static/` has no `index.html`. Registered before the catch-all mount so it wins
    for the exact `/` path; hashed assets are still served by the mount below."""
    index = _spa_dir / "index.html"
    if not index.exists():
        index = static_dir / "legacy-index.html"
    return FileResponse(str(index), media_type="text/html", headers={"Cache-Control": "no-cache"})


if _spa_dir.exists():
    app.mount("/", _NoCacheHTMLStaticFiles(directory=str(_spa_dir), html=True), name="static")
