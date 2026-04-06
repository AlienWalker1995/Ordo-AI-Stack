"""Ordo AI Stack Dashboard — unified model management and service hub."""
from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient
from pydantic import BaseModel

from dashboard.orchestration_db import get_job_counts, get_outbox_stats
from dashboard.routes_hub import router as hub_router
from dashboard.routes_orchestration import router as orchestration_router
from dashboard.services_catalog import OPS_SERVICE_MAP
from dashboard.settings import AUTH_REQUIRED as _AUTH_REQUIRED
from dashboard.settings import DASHBOARD_AUTH_TOKEN, OPENCLAW_CONFIG_PATH

# Default OpenClaw model metadata to the server cap unless a lower compaction target is set explicitly.
_ctx_raw = os.environ.get("OPENCLAW_CONTEXT_WINDOW", os.environ.get("LLAMACPP_CTX_SIZE", "262144")).strip()
OPENCLAW_CONTEXT_WINDOW = int(_ctx_raw) if _ctx_raw.isdigit() and int(_ctx_raw) > 0 else 262144

# Dashboard auth (optional bearer token only; see dashboard.settings)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if not _AUTH_REQUIRED:
        logger.warning(
            "Dashboard is running WITHOUT authentication. "
            "Set DASHBOARD_AUTH_TOKEN in .env to require Bearer auth on /api/*."
        )
    yield


app = FastAPI(title="Ordo AI Stack Dashboard", version="1.0.0", lifespan=_lifespan)
app.include_router(hub_router)
app.include_router(orchestration_router)


def _verify_auth(request: Request) -> bool:
    """Verify Authorization header. Returns True if auth passes or not required."""
    if not _AUTH_REQUIRED:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:].strip()
    return token == DASHBOARD_AUTH_TOKEN


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add CSP and security headers to reduce XSS token theft risk."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:"
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
        "/api/rag/status",
        "/api/orchestration/readiness",
    ):
        return await call_next(request)
    # /api/throughput/record: requires THROUGHPUT_RECORD_TOKEN when set (model-gateway internal; PRD §3.E)
    if path == "/api/throughput/record":
        token = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
        if token and request.headers.get("X-Throughput-Token") != token:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-Throughput-Token"})
        return await call_next(request)
    if _AUTH_REQUIRED and not _verify_auth(request):
        return JSONResponse(status_code=401, content={"detail": "Bearer token required"})
    return await call_next(request)


MODEL_GATEWAY_URL = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435").rstrip("/")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))

# Ollama library: fetched from community JSON (all pullable model:tag names)
OLLAMA_LIBRARY_URL = os.environ.get(
    "OLLAMA_LIBRARY_URL",
    "https://yuma-shintani.github.io/ollama-model-library/model.json",
)
OLLAMA_LIBRARY_CACHE_TTL = float(os.environ.get("OLLAMA_LIBRARY_CACHE_TTL_SEC", "86400"))  # 24h
_ollama_library_cache: list[str] = []
_ollama_library_ts: float = 0.0

# Fallback when fetch fails (minimal curated list)
OLLAMA_LIBRARY_FALLBACK = [
    "llama3.2", "llama3.1", "deepseek-r1:7b", "qwen2.5:7b", "qwen3:14b", "qwen3:14b-q4_K_M",
    "mistral", "nomic-embed-text", "phi4", "gemma3",
]

# Background pull status dicts
_comfyui_status: dict = {"running": False, "output": "", "done": False, "success": None}
_ollama_pull_status: dict = {"running": False, "model": "", "output": "", "pct": 0, "done": False, "success": None}



class PullRequest(BaseModel):
    model: str


# --- Ollama ---


def _fetch_ollama_library() -> list[str]:
    """Fetch pullable model names from Ollama registry. Uses community JSON; caches 24h."""
    global _ollama_library_cache, _ollama_library_ts
    now = time.monotonic()
    if _ollama_library_cache and (now - _ollama_library_ts) < OLLAMA_LIBRARY_CACHE_TTL:
        return _ollama_library_cache

    urls = [OLLAMA_LIBRARY_URL]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning("Ollama library fetch failed from %s: %s", url, e)
            continue

        names: set[str] = set()
        if isinstance(data, list):
            # yuma-shintani format: [{"name":"llama3.1","tags":[{"name":"llama3.1:8b"},...]}, ...]
            for item in data:
                if isinstance(item, dict):
                    base = (item.get("name") or "").strip()
                    tags = item.get("tags") or []
                    for t in tags:
                        if isinstance(t, dict) and t.get("name"):
                            names.add(str(t["name"]).strip())
                    if base:
                        names.add(base)  # e.g. llama3.1 -> llama3.1:latest
        elif isinstance(data, dict):
            # Official format: {"library": {"llama3.1": {"tags": ["8b","70b"]}, ...}}
            lib = data.get("library") or data
            if isinstance(lib, dict):
                for base, meta in lib.items():
                    if isinstance(meta, dict):
                        for tag in meta.get("tags") or []:
                            names.add(f"{base}:{tag}" if tag else base)
                    else:
                        names.add(base)

        if names:
            _ollama_library_cache = sorted(names)
            _ollama_library_ts = now
            return _ollama_library_cache

    _ollama_library_cache = OLLAMA_LIBRARY_FALLBACK
    _ollama_library_ts = now
    return _ollama_library_cache


@app.get("/api/ollama/library")
async def ollama_library():
    """List models available in the Ollama registry (fetched programmatically, cached 24h)."""
    models = _fetch_ollama_library()
    return {"models": models, "ok": True}


_GGUF_MODELS_DIR = Path(os.environ.get("GGUF_MODELS_DIR", "/gguf-models"))


def _scan_gguf_models() -> list[dict]:
    """Return all .gguf files on disk with their sizes."""
    models = []
    try:
        for p in sorted(_GGUF_MODELS_DIR.iterdir()):
            if p.suffix.lower() == ".gguf" and p.is_file():
                models.append({"name": p.name, "size": p.stat().st_size, "modified_at": int(p.stat().st_mtime)})
    except Exception:
        pass
    return models


@app.get("/api/ollama/models")
async def ollama_models():
    """List GGUF models available on disk (primary) merged with gateway active-model info."""
    disk_models = await asyncio.to_thread(_scan_gguf_models)
    if disk_models:
        return {"models": disk_models, "ok": True}
    # Fallback: ask model-gateway (only returns the currently loaded model)
    async with AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{MODEL_GATEWAY_URL}/api/tags")
            r.raise_for_status()
            data = r.json()
            return {"models": data.get("models", []), "ok": True}
        except Exception as e:
            return {"models": [], "ok": False, "error": str(e)}


@app.post("/api/ollama/delete")
async def ollama_delete(req: PullRequest):
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
    return {"ok": True, "message": f"Deleted '{name}' from disk."}


@app.post("/api/ollama/unload")
async def ollama_unload(req: PullRequest):
    """Unload the currently active model from the gateway without deleting GGUF files."""
    name = (req.model or "").strip()
    if not name or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid model name")
    async with AsyncClient(timeout=60.0) as client:
        try:
            r = await client.request(
                "DELETE",
                f"{MODEL_GATEWAY_URL.rstrip('/')}/api/delete",
                json={"name": name},
            )
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
            r.raise_for_status()
            return {"ok": True, "message": f"Unloaded '{name}' from the gateway."}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}") from e


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


@app.post("/api/active-model")
async def set_active_model(req: PullRequest, request: Request):
    """Unified: switch llamacpp model and keep Open WebUI + OpenClaw defaults in parity."""
    model = (req.model or "").strip()
    if not model or ".." in model or "/" in model:
        raise HTTPException(status_code=400, detail="Invalid model filename")
    if not model.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Model must be a .gguf filename")

    bare_name = model[:-5]  # strip .gguf → gateway model id
    results: dict = {}

    # 1. Switch LLAMACPP_MODEL + recreate llamacpp
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

    # 2. Update DEFAULT_MODEL + OPEN_WEBUI_DEFAULT_MODEL + recreate open-webui
    open_webui_model = _open_webui_default_model(bare_name)
    await _ops_request("POST", "/env/set", request=request,
                       json={"key": "DEFAULT_MODEL", "value": bare_name, "confirm": True})
    await _ops_request("POST", "/env/set", request=request,
                       json={"key": "OPEN_WEBUI_DEFAULT_MODEL", "value": open_webui_model, "confirm": True})
    code3, _ = await _ops_request(
        "POST", "/services/open-webui/recreate", request=request, json={"confirm": True}
    )
    results["open_webui_restarting"] = code3 in (200, 201, 202)

    # 3. Update OpenClaw agents.defaults.model.primary + model list + restart openclaw-gateway
    openclaw_model = f"gateway/{bare_name}"
    if OPENCLAW_CONFIG_PATH.exists():
        try:
            cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
            model_cfg = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})
            model_cfg["primary"] = openclaw_model
            model_cfg.setdefault("fallbacks", [])
            # Keep gateway model list to only the active model — llamacpp is single-model
            # Use bare_name (no .gguf) so the provider model ID matches agents.defaults.model.primary.
            # Mismatch causes isolated cron sessions to abort immediately (model not found in provider list).
            active_entry = _make_openclaw_model({"id": bare_name, "context_window": OPENCLAW_CONTEXT_WINDOW})
            providers = cfg.setdefault("models", {}).setdefault("providers", {})
            if "gateway" not in providers:
                providers["gateway"] = {**_OPENCLAW_GATEWAY_BASE, "models": [active_entry]}
            else:
                gw = providers["gateway"]
                if isinstance(gw, dict):
                    for k, v in _OPENCLAW_GATEWAY_BASE.items():
                        if k != "models":
                            gw[k] = v
                    gw["models"] = [active_entry]
            OPENCLAW_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            code4, _ = await _ops_request(
                "POST", "/services/openclaw-gateway/restart", request=request, json={"confirm": True}
            )
            results["openclaw_restarting"] = code4 in (200, 201)
        except Exception:
            results["openclaw_restarting"] = False
    else:
        results["openclaw_restarting"] = False

    return {"ok": True, "model": model, **results}


def _run_ollama_pull(model: str):
    """Download GGUFs via ops-controller gguf-puller (docker compose --profile models)."""
    global _ollama_pull_status
    with _state_lock:
        _ollama_pull_status = {"running": True, "model": model, "output": "", "pct": 0, "done": False, "success": None}

    repos = _normalize_gguf_pull_repos(model)
    if repos is None:
        repos = _normalize_gguf_pull_repos(_hf_url_to_ollama(model))
    if repos is None:
        msg = (
            "This stack uses GGUF files (llama.cpp), not the Ollama registry.\n\n"
            "Enter a Hugging Face repo id (e.g. bartowski/Llama-3.2-3B-Instruct-GGUF), "
            "a huggingface.co/… page or .gguf URL, hf.co/owner/repo, or type .env to pull all "
            "repos listed in GGUF_MODELS in your .env.\n\n"
            "Names like llama3.2:8b only work with a real Ollama daemon, not this gateway."
        )
        with _state_lock:
            _ollama_pull_status["output"] = msg
            _ollama_pull_status["success"] = False
            _ollama_pull_status["running"] = False
            _ollama_pull_status["done"] = True
        return

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    if not token:
        with _state_lock:
            _ollama_pull_status["output"] = "OPS_CONTROLLER_TOKEN is not set; cannot run gguf-puller from the dashboard."
            _ollama_pull_status["success"] = False
            _ollama_pull_status["running"] = False
            _ollama_pull_status["done"] = True
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
                    _ollama_pull_status["output"] = "Another model or GGUF pull is already in progress."
                    _ollama_pull_status["success"] = False
                    _ollama_pull_status["running"] = False
                    _ollama_pull_status["done"] = True
                return
            if r.status_code >= 400:
                try:
                    det = r.json().get("detail", r.text)
                except Exception:
                    det = r.text
                with _state_lock:
                    _ollama_pull_status["output"] = f"Failed to start gguf-puller: {det}"
                    _ollama_pull_status["success"] = False
                    _ollama_pull_status["running"] = False
                    _ollama_pull_status["done"] = True
                return

        with _httpx.Client(timeout=60.0) as poll_client:
            while True:
                time.sleep(1.5)
                sr = poll_client.get(
                    f"{ops_url}/models/gguf-pull/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if sr.status_code != 200:
                    continue
                st = sr.json()
                with _state_lock:
                    _ollama_pull_status["output"] = st.get("output", "")
                    _ollama_pull_status["pct"] = 50 if st.get("running") else 100
                if st.get("done"):
                    with _state_lock:
                        _ollama_pull_status["success"] = bool(st.get("success"))
                        _ollama_pull_status["running"] = False
                        _ollama_pull_status["done"] = True
                    break
    except Exception as e:
        logger.error("GGUF pull failed: %s", e)
        with _state_lock:
            _ollama_pull_status["output"] = (_ollama_pull_status.get("output") or "") + f"\nError: {e}"
            _ollama_pull_status["success"] = False
            _ollama_pull_status["running"] = False
            _ollama_pull_status["done"] = True


@app.post("/api/ollama/pull")
async def ollama_pull(req: PullRequest):
    """Start GGUF download (gguf-puller via ops-controller) in background. Poll /api/ollama/pull/status."""
    global _ollama_pull_status
    with _state_lock:
        if _ollama_pull_status.get("running"):
            raise HTTPException(status_code=409, detail="Pull already in progress")
    thread = threading.Thread(target=_run_ollama_pull, args=(req.model,), daemon=True)
    thread.start()
    return {"status": "started", "model": req.model}


@app.get("/api/ollama/pull/status")
async def ollama_pull_status():
    """Get Ollama pull progress."""
    with _state_lock:
        return dict(_ollama_pull_status)


# --- ComfyUI ---


def _scan_comfyui_models() -> list[dict]:
    """Scan ComfyUI models directory for installed files."""
    subdirs = ("checkpoints", "unet", "loras", "text_encoders", "latent_upscale_models", "vae")
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
    try:
        proc = subprocess.Popen(
            ["python3", "-u", str(script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(SCRIPTS_DIR.parent),
        )
        output_lines = []
        for line in proc.stdout:
            output_lines.append(line)
            with _state_lock:
                _comfyui_status["output"] = "".join(output_lines)
        proc.wait()
        with _state_lock:
            _comfyui_status["success"] = proc.returncode == 0
    except Exception as e:
        logger.error("ComfyUI pull (subprocess) failed: %s", e)
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
            urllib.request.urlopen(f"{COMFYUI_URL}/", timeout=5)
        except Exception:
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
        with open(config_path) as f:
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

            while True:
                time.sleep(2)
                try:
                    r = client.get(f"{COMFYUI_URL}/manager/queue/status")
                    data = r.json()
                except Exception:
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


COMFYUI_CATEGORIES = ("checkpoints", "unet", "loras", "text_encoders", "latent_upscale_models", "vae")


@app.delete("/api/comfyui/models/{category}/{filename:path}")
async def comfyui_delete(category: str, filename: str):
    """Delete a ComfyUI model file. category: checkpoints, loras, text_encoders, latent_upscale_models, vae."""
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
        return {"ok": True, "message": f"Deleted {category}/{filename}"}
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


@app.get("/api/comfyui/models")
async def comfyui_models():
    """List ComfyUI models on disk."""
    try:
        models = _scan_comfyui_models()
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
        import json as _json
        config = _json.loads(config_path.read_text(encoding="utf-8"))
        default_quant = config.get("defaults", {}).get("quant", "Q4_K_M")
        try:
            installed = {(m["category"], m["name"]) for m in _scan_comfyui_models()}
        except Exception:
            installed = set()
        packs = {}
        for name, pack in config.get("packs", {}).items():
            models = pack.get("models", [])
            installed_count = sum(
                1 for m in models
                if (m.get("dest", "checkpoints"), Path(m["file"].replace("{quant}", default_quant)).name) in installed
            )
            packs[name] = {
                "description": pack.get("description", ""),
                "model_count": len(models),
                "installed_count": installed_count,
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
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Set confirm: true to execute")
    code, data = await _ops_request(
        "POST",
        "/comfyui/install-node-requirements",
        request=request,
        json={"node_path": body.node_path.strip(), "confirm": True},
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

    None means the string is not suitable (e.g. Ollama-style ``llama3.2:8b``).
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


def _hf_url_to_ollama(raw: str) -> str:
    """Convert a HuggingFace GGUF URL to Ollama's hf.co/owner/repo format.
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
    - GGUF / HF repo → background gguf-puller via ops (same as ``/api/ollama/pull``); poll ``/api/ollama/pull/status``.
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
            if _ollama_pull_status.get("running"):
                raise HTTPException(status_code=409, detail="Pull already in progress")
        thread = threading.Thread(target=_run_ollama_pull, args=(raw,), daemon=True)
        thread.start()
        return {
            "status": "started",
            "target": "gguf",
            "message": "Poll /api/ollama/pull/status for progress.",
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


MCP_GATEWAY_SERVERS = os.environ.get("MCP_GATEWAY_SERVERS", "duckduckgo,n8n,tavily,comfyui")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG_PATH")
# Suggested servers (dropdown). Users can also add any valid server name via custom input.
MCP_CATALOG = [
    "duckduckgo", "n8n", "tavily", "comfyui", "fetch", "dockerhub", "github-official",
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
        initial = ",".join(s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()) or "duckduckgo,n8n,tavily,comfyui"
        path.write_text(initial)
        return [s.strip() for s in initial.split(",") if s.strip()]
    return [s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()]


def _write_mcp_servers(servers: list[str]) -> Path:
    """Write servers to config file. Raises if not in dynamic mode."""
    path = _mcp_config_path()
    if not path:
        raise HTTPException(status_code=409, detail="MCP config not in dynamic mode (no volume)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(servers))
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


MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "http://mcp-gateway:8811")


def _get_active_mcp_servers() -> list[str]:
    """Get enabled MCP servers from configuration file."""
    try:
        return _read_mcp_servers()
    except Exception:
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
        async with AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{MCP_GATEWAY_URL.rstrip('/')}/mcp",
                headers={"X-Client-ID": "dashboard"},
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
    - Docker Hub URL: https://hub.docker.com/mcp/server/hugging-face/overview
    - Raw server name: hugging-face, fetch, mcp/firecrawl
    """
    s = raw.strip()
    if not s:
        return None
    # Docker Hub MCP URL: hub.docker.com/mcp/server/<server-id>/...
    if "hub.docker.com" in s and "/mcp/server/" in s:
        try:
            # Extract segment after /mcp/server/
            idx = s.find("/mcp/server/")
            if idx >= 0:
                rest = s[idx + len("/mcp/server/"):]
                server_id = rest.split("/")[0].split("?")[0]
                if server_id and _valid_mcp_server_name(server_id):
                    return server_id
        except (IndexError, ValueError):
            pass
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
    _write_mcp_servers(servers)
    return {"status": "added", "servers": servers}


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
    _write_mcp_servers(servers)
    return {"status": "removed", "servers": servers}


# --- Token Throughput ---

# In-memory store: model -> list of output_tokens_per_sec (rolling, max 500)
_throughput_samples: dict[str, list[float]] = {}
_ttft_samples: dict[str, list[float]] = {}
_MAX_SAMPLES_PER_MODEL = 500

# Last benchmark result (persists across page refresh until dashboard restart)
_last_benchmark: dict | None = None

# Service usage: list of { model, service, tps, ts } for "which service uses which model"
_service_usage: list[dict] = []
_MAX_SERVICE_USAGE = 500

DASHBOARD_DATA_PATH = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
DASHBOARD_DATA_PATH.mkdir(parents=True, exist_ok=True)
_THROUGHPUT_FILE = DASHBOARD_DATA_PATH / "throughput.json"


def _load_throughput_state() -> None:
    """Load throughput samples and last benchmark from disk (R4)."""
    global _throughput_samples, _ttft_samples, _last_benchmark, _service_usage
    if not _THROUGHPUT_FILE.exists():
        return
    try:
        data = json.loads(_THROUGHPUT_FILE.read_text())
        _throughput_samples = {k: v for k, v in (data.get("samples") or {}).items() if isinstance(v, list)}
        _ttft_samples = {k: v for k, v in (data.get("ttft_samples") or {}).items() if isinstance(v, list)}
        _last_benchmark = data.get("last_benchmark") if isinstance(data.get("last_benchmark"), dict) else None
        _service_usage = [u for u in (data.get("service_usage") or []) if isinstance(u, dict)][-_MAX_SERVICE_USAGE:]
    except Exception as e:
        logger.warning("Throughput state load failed: %s", e)


def _save_throughput_state() -> None:
    """Persist throughput state to disk."""
    try:
        _THROUGHPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _THROUGHPUT_FILE.write_text(json.dumps({
            "samples": _throughput_samples,
            "ttft_samples": _ttft_samples,
            "last_benchmark": _last_benchmark,
            "service_usage": _service_usage[-_MAX_SERVICE_USAGE:],
        }))
    except Exception as e:
        logger.warning("Throughput state save failed: %s", e)


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
    model: str = ""
    output_tokens_per_sec: float = 0.0
    service: str = ""
    ttft_ms: float = 0.0


@app.post("/api/throughput/record")
async def throughput_record(req: ThroughputRecordRequest):
    """Record a throughput sample from real-world usage (e.g. model gateway). Fire-and-forget."""
    model = req.model.strip()
    if not model or req.output_tokens_per_sec <= 0:
        return {"ok": True}
    with _state_lock:
        if model not in _throughput_samples:
            _throughput_samples[model] = []
        _throughput_samples[model].append(req.output_tokens_per_sec)
        if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
            _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        if req.ttft_ms > 0:
            if model not in _ttft_samples:
                _ttft_samples[model] = []
            _ttft_samples[model].append(req.ttft_ms)
            if len(_ttft_samples[model]) > _MAX_SAMPLES_PER_MODEL:
                _ttft_samples[model] = _ttft_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        # Service usage (which service is taxing which model)
        service = (req.service or "unknown").strip()[:64]
        _service_usage.append({
            "model": model,
            "service": service,
            "tps": round(req.output_tokens_per_sec, 1),
            "ttft_ms": round(req.ttft_ms, 1) if req.ttft_ms > 0 else 0.0,
            "ts": time.time(),
        })
        if len(_service_usage) > _MAX_SERVICE_USAGE:
            _service_usage[:] = _service_usage[-_MAX_SERVICE_USAGE:]
        _save_throughput_state()
    return {"ok": True}


@app.get("/api/throughput/service-usage")
async def throughput_service_usage():
    """Return recent service usage: which service used which model (from model gateway traffic)."""
    now = time.time()
    # Last 24h, grouped by model -> services
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
            by_svc[s].append({"tps": u["tps"], "ts": u["ts"]})
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


@app.get("/api/throughput/stats")
async def throughput_stats():
    """Return per-model throughput stats: peak, p50, p95, p99, latest, sample_count. Includes last_benchmark if available."""
    result: dict[str, dict] = {}
    with _state_lock:
        snapshot = {m: list(s) for m, s in _throughput_samples.items()}
        ttft_snapshot = {m: list(s) for m, s in _ttft_samples.items()}
        benchmark = dict(_last_benchmark) if _last_benchmark else None
    for model, samples in snapshot.items():
        if not samples:
            continue
        sorted_s = sorted(samples)
        ttfts = ttft_snapshot.get(model, [])
        sorted_ttfts = sorted(ttfts)
        result[model] = {
            "latest": round(samples[-1], 1),
            "peak": round(max(samples), 1),
            "p50": round(_percentile(sorted_s, 50), 1),
            "p95": round(_percentile(sorted_s, 95), 1),
            "p99": round(_percentile(sorted_s, 99), 1),
            "ttft_p50_ms": round(_percentile(sorted_ttfts, 50), 1) if sorted_ttfts else 0.0,
            "ttft_p95_ms": round(_percentile(sorted_ttfts, 95), 1) if sorted_ttfts else 0.0,
            "sample_count": len(samples),
        }
    out: dict = {"models": result, "ok": True}
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
        sorted_s = sorted(samples)
        ttfts = ttft_snapshot.get(model, [])
        sorted_ttfts = sorted(ttfts)
        top_models.append(
            {
                "model": model,
                "latest_tps": round(samples[-1], 1),
                "p95_tps": round(_percentile(sorted_s, 95), 1),
                "latest_ttft_ms": round(ttfts[-1], 1) if ttfts else 0.0,
                "p95_ttft_ms": round(_percentile(sorted_ttfts, 95), 1) if sorted_ttfts else 0.0,
                "sample_count": len(samples),
            }
        )
    top_models.sort(key=lambda item: item["sample_count"], reverse=True)
    try:
        rag = await asyncio.wait_for(rag_status(), timeout=2.0)
    except TimeoutError:
        rag = {"ok": False, "error": "timeout"}
    return {
        "ok": True,
        "llamacpp_ctx_size": int(os.environ.get("LLAMACPP_CTX_SIZE", "262144") or 262144),
        "openclaw_context_window": OPENCLAW_CONTEXT_WINDOW,
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


@app.get("/api/ollama/ps")
async def ollama_ps():
    """List models currently loaded in Ollama (via model-gateway)."""
    async with AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{MODEL_GATEWAY_URL.rstrip('/')}/api/ps")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}")


# Embedding models don't support /api/generate — exclude from throughput benchmark
_EMBED_MODEL_PATTERNS = ("embed", "bge", "mxbai", "arctic-embed", "granite-embedding", "paraphrase-multilingual")


def _is_embedding_model(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _EMBED_MODEL_PATTERNS)


@app.post("/api/throughput/benchmark")
async def throughput_benchmark(req: ThroughputBenchmarkRequest):
    """Run a quick benchmark via model-gateway /api/generate (llama.cpp). Returns tokens/sec and related metrics."""
    model = req.model.strip() or "llama3.2"
    if _is_embedding_model(model):
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is an embedding model and does not support text generation. Choose an LLM (e.g. llama3.2, deepseek-r1:7b).",
        )
    prompt = "Say 'ok' and nothing else."
    url = f"{MODEL_GATEWAY_URL.rstrip('/')}/api/generate"
    async with AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(url, json={"model": model, "prompt": prompt, "stream": False})
            if r.status_code == 400:
                try:
                    err = r.json()
                    msg = err.get("error", r.text) or "Bad request"
                except Exception:
                    msg = r.text or "Bad request"
                raise HTTPException(status_code=400, detail=f"Ollama: {msg}")
            r.raise_for_status()
            data = r.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}")

    eval_count = data.get("eval_count", 0)
    eval_duration_ns = data.get("eval_duration", 0) or 1
    prompt_eval_count = data.get("prompt_eval_count", 0)
    prompt_eval_duration_ns = data.get("prompt_eval_duration", 0) or 1
    load_duration_ns = data.get("load_duration", 0)
    total_duration_ns = data.get("total_duration", 0)

    eval_duration_sec = eval_duration_ns / 1e9
    prompt_eval_duration_sec = prompt_eval_duration_ns / 1e9

    output_tokens_per_sec = eval_count / eval_duration_sec if eval_duration_sec > 0 else 0
    input_tokens_per_sec = prompt_eval_count / prompt_eval_duration_sec if prompt_eval_duration_sec > 0 else 0

    # Store sample for stats (peak, percentiles)
    with _state_lock:
        if model not in _throughput_samples:
            _throughput_samples[model] = []
        _throughput_samples[model].append(output_tokens_per_sec)
        if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
            _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        _save_throughput_state()

    payload = {
        "ok": True,
        "model": model,
        "prompt_tokens": prompt_eval_count,
        "output_tokens": eval_count,
        "output_tokens_per_sec": round(output_tokens_per_sec, 1),
        "input_tokens_per_sec": round(input_tokens_per_sec, 1),
        "eval_duration_ms": round(eval_duration_ns / 1e6, 1),
        "load_duration_ms": round(load_duration_ns / 1e6, 1),
        "total_duration_ms": round(total_duration_ns / 1e6, 1),
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
        async with AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers=headers, **kwargs)
            try:
                data = r.json()
            except Exception:
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
    # Ollama allows namespaced ids: owner/model:tag (slashes required). Only reject empty / traversal.
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

    # 3. Restart openclaw-gateway (re-reads model config on startup)
    code3, _ = await _ops_request(
        "POST", "/services/openclaw-gateway/restart", request=request, json={"confirm": True}
    )

    return {
        "ok": code2 in (200, 201),
        "model": name,
        "open_webui_model": open_webui_model,
        "webui_recreated": code2 in (200, 201),
        "openclaw_restarted": code3 in (200, 201),
        "webui_error": data2.get("detail") if code2 >= 400 else None,
    }


# --- OpenClaw model management ---

# Gateway provider base written into openclaw.json (must match merge_gateway_config.py)
_OPENCLAW_GATEWAY_BASE = {
    "baseUrl": "http://model-gateway:11435/v1",
    "apiKey": "local",
    "api": "openai-responses",
    "headers": {"X-Service-Name": "openclaw"},
}


def _make_openclaw_model(item: dict) -> dict:
    """Transform a /v1/models entry into an OpenClaw model definition."""
    mid = item.get("id", "")
    name = mid.split("/")[-1] if "/" in mid else mid
    name = name.replace(":", " ").replace("-", " ").replace(".", " ")
    name = " ".join(w.capitalize() for w in name.split())
    if item.get("profile") == "chat":
        name = f"{name} Chat"
    lower = mid.lower()
    has_vision = "vision" in lower or "llava" in lower or "puppy" in lower
    is_reasoning = "r1" in lower or "reasoning" in lower or "qwen3" in lower or "qwen-3" in lower
    context_window = item.get("context_window")
    if not isinstance(context_window, int) or context_window <= 0:
        context_window = OPENCLAW_CONTEXT_WINDOW
    return {
        "id": mid,
        "name": name,
        "reasoning": is_reasoning,
        "input": ["text", "image"] if has_vision else ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": context_window,
        "maxTokens": 8192,
    }


@app.get("/api/openclaw/models")
async def get_openclaw_models():
    """Return OpenClaw's current model list from openclaw.json."""
    if not OPENCLAW_CONFIG_PATH.exists():
        return {"models": [], "count": 0, "config_found": False}
    try:
        cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
        gw = cfg.get("models", {}).get("providers", {}).get("gateway", {})
        models = gw.get("models", [])
        return {"models": models, "count": len(models), "config_found": True}
    except Exception as e:
        return {"models": [], "count": 0, "config_found": True, "error": str(e)}


@app.get("/api/openclaw/default-model")
async def get_openclaw_default_model():
    """Return the current OpenClaw agent default model (agents.defaults.model.primary)."""
    if not OPENCLAW_CONFIG_PATH.exists():
        return {"default_model": "", "config_found": False}
    try:
        cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
        primary = (
            cfg.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "")
        )
        return {"default_model": primary or "", "config_found": True}
    except Exception as e:
        return {"default_model": "", "config_found": True, "error": str(e)}


class OpenClawDefaultModelRequest(BaseModel):
    model: str


@app.post("/api/openclaw/default-model")
async def set_openclaw_default_model(req: OpenClawDefaultModelRequest, request: Request):
    """Write agents.defaults.model.primary to openclaw.json and restart openclaw-gateway."""
    if not OPENCLAW_CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="openclaw.json not found — is OpenClaw set up?")
    model = (req.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    try:
        cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read openclaw.json: {e}")

    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    model_cfg = defaults.setdefault("model", {})
    model_cfg["primary"] = model
    if "fallbacks" not in model_cfg:
        model_cfg["fallbacks"] = []

    try:
        OPENCLAW_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot write openclaw.json: {e}")

    code, _ = await _ops_request(
        "POST", "/services/openclaw-gateway/restart", request=request, json={"confirm": True}
    )

    return {
        "ok": True,
        "model": model,
        "openclaw_restarted": code in (200, 201),
    }


@app.post("/api/openclaw/sync")
async def sync_openclaw_models(request: Request):
    """Fetch current models from model-gateway, update openclaw.json, restart openclaw-gateway."""
    if not OPENCLAW_CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="openclaw.json not found — is OpenClaw set up?")

    # Fetch live model list from model-gateway
    try:
        async with AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{MODEL_GATEWAY_URL}/v1/models")
            r.raise_for_status()
            raw = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach model-gateway: {e}")

    items = raw.get("data", []) if isinstance(raw, dict) else []
    # Skip ollama/-prefixed duplicates; bare IDs route fine through the gateway
    new_models = [_make_openclaw_model(m) for m in items if m.get("id") and not m["id"].startswith("ollama/")]

    # Read + patch openclaw.json
    try:
        cfg = json.loads(OPENCLAW_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read openclaw.json: {e}")

    providers = cfg.setdefault("models", {}).setdefault("providers", {})

    # Strip any per-model baseUrl/apiKey (OpenClaw 2026.2.x rejects them)
    for pv in providers.values():
        if isinstance(pv, dict):
            for m in (pv.get("models") or []):
                if isinstance(m, dict):
                    m.pop("baseUrl", None)
                    m.pop("apiKey", None)

    # Upsert gateway provider
    if "gateway" not in providers:
        providers["gateway"] = {**_OPENCLAW_GATEWAY_BASE, "models": new_models}
    else:
        gw = providers["gateway"]
        if isinstance(gw, dict):
            for k, v in _OPENCLAW_GATEWAY_BASE.items():
                if k != "models":
                    gw[k] = v
            gw["models"] = new_models

    try:
        OPENCLAW_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot write openclaw.json: {e}")

    # Restart openclaw-gateway to pick up the updated model list
    code, _ = await _ops_request(
        "POST", "/services/openclaw-gateway/restart", request=request, json={"confirm": True}
    )

    return {
        "ok": True,
        "synced_count": len(new_models),
        "models": [m["id"] for m in new_models],
        "openclaw_restarted": code in (200, 201),
    }


# --- RAG ---

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
RAG_COLLECTION = os.environ.get("RAG_COLLECTION", "documents")


@app.get("/api/rag/status")
async def rag_status():
    """Qdrant health and document collection stats. No auth required."""
    try:
        async with AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{QDRANT_URL}/collections/{RAG_COLLECTION}")
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

BASE_PATH_ENV = os.environ.get("BASE_PATH", "/")


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

    gpu = None
    try:
        import pynvml  # optional; only present when nvidia-ml-py is installed
        pynvml.nvmlInit()
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            mi = pynvml.nvmlDeviceGetMemoryInfo(h)
            ut = pynvml.nvmlDeviceGetUtilizationRates(h)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace").strip()
            else:
                name = str(name).strip()
            gpu = _nvml_vram_to_gpu_dict(name, int(mi.used), int(mi.total), ut.gpu)
        finally:
            pynvml.nvmlShutdown()
    except Exception as e:
        logger.debug("GPU stats unavailable: %s", e)

    return {
        "cpu_pct": cpu_pct,
        "ram_used_gb": round(mem.used / 1e9, 1),
        "ram_total_gb": round(mem.total / 1e9, 1),
        "ram_pct": mem.percent,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
        "gpu": gpu,
    }


# --- Static ---

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
