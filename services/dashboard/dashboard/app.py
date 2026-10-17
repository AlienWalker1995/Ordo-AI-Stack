"""Ordo AI Stack Dashboard — unified model management and service hub."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

# Lock for shared mutable state accessed from both async handlers and background threads
_state_lock = threading.Lock()

import psutil

logger = logging.getLogger(__name__)

import httpx as _httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from dashboard import auth
from dashboard.console import CHAT_MODEL_SUFFIXES
from dashboard.routes_auth import router as auth_router
from dashboard.routes_console import router as console_router
from dashboard.routes_hub import router as hub_router
from dashboard.routes_orchestration import router as orchestration_router
from dashboard.secret_env import read_secret
from dashboard.services_catalog import OPS_SERVICE_MAP

# Persistent httpx client — connection pooling avoids per-request TCP handshake overhead.
_http_client: _httpx.AsyncClient | None = None


def _get_http_client() -> _httpx.AsyncClient:
    """Return the shared async HTTP client (created in lifespan)."""
    assert _http_client is not None, "HTTP client not initialised — is lifespan running?"
    return _http_client


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _http_client
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
app.include_router(auth_router)
app.include_router(console_router)
app.include_router(hub_router)
app.include_router(orchestration_router)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions — log the traceback but return a safe 500 to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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
    """Require a principal for state-changing and ops-forwarding /api/* routes (dashboard/auth.py)."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    # /api/throughput/record: requires THROUGHPUT_RECORD_TOKEN when set (model-gateway internal; PRD §3.E)
    if path == "/api/throughput/record":
        token = read_secret("THROUGHPUT_RECORD_TOKEN")
        if token and not hmac.compare_digest(request.headers.get("X-Throughput-Token", ""), token):
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-Throughput-Token"})
        return await call_next(request)
    if not auth.requires_principal(request.method, path):
        return await call_next(request)
    if await auth.request_principal(request) is None:
        peer_ip = request.client.host if request.client else None
        logger.warning("AUTH_FAIL path=%s method=%s src=%s", path, request.method, peer_ip or "unknown")
        how = "Sign in with the link `ordo up` printed" if auth.local_mode() else "Sign in through the SSO edge"
        return JSONResponse(
            status_code=401,
            content={"detail": f"{how}, or send Authorization: Bearer <OPS_CONTROLLER_TOKEN>"},
        )
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
MODEL_GATEWAY_API_KEY = read_secret("MODEL_GATEWAY_API_KEY") or read_secret("LITELLM_MASTER_KEY")


def _model_gateway_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if MODEL_GATEWAY_API_KEY:
        headers["Authorization"] = f"Bearer {MODEL_GATEWAY_API_KEY}"
    return headers

# --- LLM (llama.cpp / GGUF) ---


_GGUF_MODELS_DIR = Path(os.environ.get("GGUF_MODELS_DIR", "/gguf-models"))


def _scan_gguf_models() -> list[dict]:
    """Return every chat model file on disk (.gguf and .ninfer) with its size."""
    models = []
    try:
        for p in sorted(_GGUF_MODELS_DIR.iterdir()):
            if p.suffix.lower() in CHAT_MODEL_SUFFIXES and p.is_file():
                st = p.stat()
                models.append({"name": p.name, "size": st.st_size, "modified_at": int(st.st_mtime)})
    except OSError as e:
        logger.warning("GGUF model scan failed: %s", e)
    return models


# --- Token Throughput ---

_throughput_samples: dict[str, list[dict]] = {}   # {"tps": float, "ts": epoch}
_ttft_samples: dict[str, list[dict]] = {}         # {"ms": float, "ts": epoch}
_MAX_SAMPLES_PER_MODEL = 500
_MAX_TRACKED_MODELS = 50
# v2: samples are timestamped dicts. v1 stored bare floats keyed largely by routing
# ALIAS (one `local-chat` bucket conflating every model ever active behind it, CPU
# failover included) — unattributable, so version bumps trigger a clean reset.
# v3: tok/s is the engine's generation speed. v2 divided by the whole request, queue and prompt
# reading included, so a 206 tok/s GPU read as 17 tok/s while requests queued.
_THROUGHPUT_STORE_VERSION = 3
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

DASHBOARD_DATA_PATH = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
DASHBOARD_DATA_PATH.mkdir(parents=True, exist_ok=True)
_THROUGHPUT_FILE = DASHBOARD_DATA_PATH / "throughput.json"


def _load_throughput_state() -> None:
    """Load throughput samples and last benchmark from disk (R4). v1 files (no
    version field) get a clean reset — their samples are un-timestamped and
    alias-conflated; only last_benchmark carries over."""
    global _throughput_samples, _ttft_samples, _last_benchmark
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
            _save_throughput_state()
            return
        _throughput_samples = {
            k: [s for s in v if isinstance(s, dict) and "tps" in s and "ts" in s]
            for k, v in (data.get("samples") or {}).items() if isinstance(v, list)
        }
        _ttft_samples = {
            k: [s for s in v if isinstance(s, dict) and "ms" in s and "ts" in s]
            for k, v in (data.get("ttft_samples") or {}).items() if isinstance(v, list)
        }
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
        _maybe_save_throughput()
    return {"ok": True}


# Authoritative active model, from the same ops-controller /model-config the Model
# Control tab uses. Cached (positive AND negative) so a 10s-poll dashboard doesn't
# hammer ops; control_plane_ok=False means "unreachable" and the UI says so instead
# of guessing. Reachable-but-unconfigured (file=None, ok=True) is NOT an error.
_ACTIVE_MODEL_CACHE_TTL = 30.0
_active_model_cache: dict = {"checked": 0.0, "value": None}
_active_model_fetch_lock = asyncio.Lock()


def _gateway_pin_alias(gguf: str | None) -> str | None:
    """Gateway pin-alias for a GGUF — same derivation as the model-gateway
    entrypoint (basename, .gguf stripped, lowercased). Derived HERE, in one
    place, so no UI re-implements it."""
    if not gguf:
        return None
    name = gguf.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in CHAT_MODEL_SUFFIXES:   # the entrypoint strips .gguf or .ninfer the same way
        name = name.removesuffix(suffix)
    return name


async def _throughput_active_model() -> dict:
    """Return {"ok": bool, "file": str | None} — ok reflects control-plane
    reachability (HTTP 200 with a dict body), file is the active GGUF or None
    when reachable but unconfigured. Cached (positive AND negative)."""
    now = time.monotonic()
    if now - _active_model_cache["checked"] < _ACTIVE_MODEL_CACHE_TTL:
        return _active_model_cache["value"]
    async with _active_model_fetch_lock:
        # Re-check: a concurrent caller may have refreshed while we waited.
        now = time.monotonic()
        if now - _active_model_cache["checked"] < _ACTIVE_MODEL_CACHE_TTL:
            return _active_model_cache["value"]
        code, data = await _ops_request("GET", "/model-config", timeout=3.0)
        if code == 200 and isinstance(data, dict):
            # `active_file`, not `active_model`: samples are keyed by GGUF file and
            # active_model is a catalog id (the v2 contract).
            value = {"ok": True, "file": str(data["active_file"]) if data.get("active_file") else None}
        else:
            logger.warning("throughput active-model fetch failed (HTTP %s)", code)
            value = {"ok": False, "file": None}
        _active_model_cache["checked"] = now
        _active_model_cache["value"] = value
        return value


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
    active = await _throughput_active_model()
    out: dict = {
        "models": result,
        "ok": True,
        "active_model": active["file"],
        "active_model_alias": _gateway_pin_alias(active["file"]),
        "control_plane_ok": active["ok"],
    }
    if benchmark:
        out["last_benchmark"] = benchmark
    return out


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
OPS_CONTROLLER_TOKEN = read_secret("OPS_CONTROLLER_TOKEN")


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
    # X-Actor names the caller in ops-controller's audit log; a caller may override it.
    headers = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}", "X-Actor": "dashboard", **extra}
    try:
        r = await _get_http_client().request(method, url, headers=headers, timeout=timeout, **kwargs)
        try:
            data = r.json()
        except (ValueError, UnicodeDecodeError):
            data = {"detail": r.text or "Unknown error"}
        return r.status_code, data
    except _httpx.TimeoutException:
        detail = f"ops-controller did not answer {method} {path} within {timeout:g} s"
        if method != "GET":
            detail += "; the action may still be running, so check the service before retrying"
        return 503, {"detail": detail}
    except Exception as e:
        return 503, {"detail": str(e) or type(e).__name__}


# One docker start/stop/restart is 60 s at most in ops-controller (DockerBackend), and an owner
# with netns members runs one more per member, so a lifecycle call outlasts several of them.
_LIFECYCLE_TIMEOUT = 180.0
_LOGS_TIMEOUT = 15.0


def _ops_error_detail(code: int, data: dict) -> str:
    """The reason a failed ops-controller call gives. ops-controller answers `{"error": ...}`
    (ordo/control/responses.py) and `_ops_request` its own failures as `{"detail": ...}`; a body
    with neither is passed on as its JSON text and logged, never dropped."""
    for key in ("detail", "error"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value:
            return value
    logger.warning("ops-controller answered %s without a reason: %r", code, data)
    return f"ops-controller answered HTTP {code}: {json.dumps(data)}"


async def _ops_proxy(method: str, path: str, request: Request, *, timeout: float, **kwargs) -> dict:
    """An ops-controller call whose failure reaches the page as an HTTPException with its reason."""
    code, data = await _ops_request(method, path, request=request, timeout=timeout, **kwargs)
    if code >= 400:
        raise HTTPException(status_code=code, detail=_ops_error_detail(code, data))
    return data


@app.post("/api/ops/services/{service_id}/start")
async def ops_start(service_id: str, request: Request):
    """Start a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    return await _ops_proxy(
        "POST", f"/services/{ops_id}/start", request, timeout=_LIFECYCLE_TIMEOUT, json={"confirm": True}
    )


@app.post("/api/ops/services/{service_id}/stop")
async def ops_stop(service_id: str, request: Request):
    """Stop a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    return await _ops_proxy(
        "POST", f"/services/{ops_id}/stop", request, timeout=_LIFECYCLE_TIMEOUT, json={"confirm": True}
    )


@app.post("/api/ops/services/{service_id}/restart")
async def ops_restart(service_id: str, request: Request):
    """Restart a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    return await _ops_proxy(
        "POST", f"/services/{ops_id}/restart", request, timeout=_LIFECYCLE_TIMEOUT, json={"confirm": True}
    )


@app.get("/api/ops/services/{service_id}/logs")
async def ops_logs(service_id: str, request: Request, tail: int = 100):
    """Get service logs via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    return await _ops_proxy("GET", f"/services/{ops_id}/logs?tail={tail}", request, timeout=_LOGS_TIMEOUT)


# A container of a managed project (another compose project listed in `managed_projects:`):
# ordo/control/managed_projects.py answers its logs and a confirmed, rate-limited restart.
@app.get("/api/ops/projects/{project}/containers/{name}/logs")
async def ops_project_container_logs(project: str, name: str, request: Request, tail: int = 100):
    """Get a managed-project container's logs via ops controller."""
    path = f"/projects/{quote(project, safe='')}/containers/{quote(name, safe='')}/logs?tail={tail}"
    return await _ops_proxy("GET", path, request, timeout=_LOGS_TIMEOUT)


@app.post("/api/ops/projects/{project}/containers/{name}/restart")
async def ops_project_container_restart(project: str, name: str, request: Request):
    """Restart a managed-project container via ops controller."""
    path = f"/projects/{quote(project, safe='')}/containers/{quote(name, safe='')}/restart"
    return await _ops_proxy("POST", path, request, timeout=_LIFECYCLE_TIMEOUT, json={"confirm": True})


# --- RAG ---

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
RAG_COLLECTION = os.environ.get("RAG_COLLECTION", "documents")


async def rag_status():
    """Qdrant health and document collection stats."""
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


def _mib_to_gb(mib: int | None) -> float | None:
    """MiB -> the decimal GB the UI shows (no client-side byte math)."""
    if mib is None:
        return None
    return round(mib * 1024 * 1024 / 1e9, 1)


async def _live_gpus() -> list[dict]:
    """ops-controller `GET /gpus`, the stack's one live GPU reader (ordo/render/gpu_live.py), in
    the /api/hardware `gpus` shape. The dashboard reserves no GPU and never probes one itself.
    ops-controller unreachable or answering garbage -> [] (the GPU widgets show none)."""
    code, data = await _ops_request("GET", "/gpus", timeout=15.0)
    if code != 200 or not isinstance(data, dict):
        logger.debug("ops-controller /gpus answered %s: %s", code, data)
        return []
    gpus = []
    for card in data.get("gpus") or []:
        gpus.append({
            "index": card.get("index"),
            "uuid": card.get("uuid"),
            "name": card.get("name"),
            "vram_total_gb": _mib_to_gb(card.get("vram_total_mib")),
            "vram_used_gb": _mib_to_gb(card.get("vram_used_mib")),
            "vram_total_mib": card.get("vram_total_mib"),
            "utilization_pct": card.get("utilization_pct"),
            "temp_c": card.get("temp_c"),
        })
    return gpus


def _primary_gpu(gpus: list[dict]) -> dict | None:
    """The hw-stat bar's single `gpu`: card index 0 (the first card when none is numbered 0)."""
    if not gpus:
        return None
    card = next((g for g in gpus if g["index"] == 0), gpus[0])
    return {
        "name": card["name"] or "GPU",
        "vram_used_gb": card["vram_used_gb"],
        "vram_total_gb": card["vram_total_gb"],
        "utilization_pct": card["utilization_pct"],
        # False: the used reading was not credible (see gpu_live), so the UI shows N/A, not 100%.
        "memory_reading_reliable": card["vram_used_gb"] is not None,
        "source": "ops-controller",
    }


async def hardware_stats():
    """System resource stats. Blocking calls run in thread pool (R7)."""
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

    gpus = await _live_gpus()
    gpu = _primary_gpu(gpus)

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
    """Per-service compute pressure (CPU/RAM). A value nobody measured is null with a `reason`,
    never 0: a zero reads as "idle", which is a claim this endpoint cannot make for it."""
    from dashboard.services_catalog import OPS_SERVICE_MAP, SERVICES

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = read_secret("OPS_CONTROLLER_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    host_info = {
        "cpu_cores": psutil.cpu_count() or 0,
        "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
    }

    def _row(service_id: str, cat: dict | None, name: str, *, running: bool | None, reason: str | None,
             cpu_pct: float | None = None, mem_gb: float | None = None, mem_pct: float | None = None) -> dict:
        # vram_gb/vram_pct stay null: ops-controller does not measure per-container VRAM yet.
        return {
            "id": service_id, "name": (cat or {}).get("name") or name,
            "cpu_pct": cpu_pct, "mem_gb": mem_gb, "mem_pct": mem_pct,
            "vram_gb": None, "vram_pct": None,
            "has_gpu": bool((cat or {}).get("has_gpu", False)),
            "running": running, "reason": reason,
        }

    def _unavailable_payload(reason: str):
        services_out = [_row(s["id"], s, s["id"], running=None, reason="source unavailable") for s in SERVICES]
        return {"gpu": None, "host": host_info, "services": services_out, "vram_aggregate_unavailable": True,
                "unavailable": True, "reason": reason}

    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{ops_url}/stats/services", headers=headers)
            if r.status_code != 200:
                return _unavailable_payload(f"ops-controller answered HTTP {r.status_code}")
            raw = r.json()
    except (_httpx.RequestError, OSError) as e:
        logger.debug("service-pressure: ops-controller unreachable: %s", e)
        return _unavailable_payload("ops-controller is not answering")

    def _measured(value) -> float | None:
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    raw_services: dict = raw.get("services") or {}
    catalog = {s["id"]: s for s in SERVICES}
    compose_to_display = {v: k for k, v in OPS_SERVICE_MAP.items()}

    services_out: list[dict] = []
    for compose_id, row in raw_services.items():
        display_id = compose_to_display.get(compose_id, compose_id)
        cat = catalog.get(display_id)
        if row.get("running") is True:
            services_out.append(_row(display_id, cat, compose_id, running=True, reason=None,
                                     cpu_pct=_measured(row.get("cpu_pct")), mem_gb=_measured(row.get("mem_gb")),
                                     mem_pct=_measured(row.get("mem_pct"))))
        else:
            services_out.append(_row(display_id, cat, compose_id, running=False, reason="container not running"))
    seen = {s["id"] for s in services_out}
    for cid, cat in catalog.items():
        if cid not in seen:
            services_out.append(_row(cid, cat, cid, running=None, reason="not measured"))

    def _pressure(service: dict) -> float:
        measured = [v for v in (service["cpu_pct"], service["mem_pct"]) if v is not None]
        return max(measured, default=-1.0)

    services_out.sort(key=_pressure, reverse=True)
    return {
        "gpu": raw.get("gpu"),
        "host": host_info,
        "services": services_out,
        "vram_aggregate_unavailable": bool(raw.get("vram_aggregate_unavailable", False)),
        "unavailable": False,
        "reason": None,
    }


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


frontend_dist = Path(__file__).parent / "frontend" / "dist"

# The dashboard SPA is the React build in frontend/dist (the production image builds it; locally,
# `npm run build` in frontend/). A production Vite build emits hashed ES modules referenced with
# script-src 'self', so it satisfies the app's strict CSP. All /api/* and /grafana/* routes are
# registered above and take precedence over the catch-all static mount.

# Served at / when the frontend has not been built (a bare checkout, CI): an honest page saying
# so, rather than a 404 or a stale fallback UI.
_UNBUILT_SHELL = (
    "<!doctype html><meta charset=utf-8><title>Ordo</title>"
    "<p>The dashboard frontend has not been built. Run <code>npm run build</code> in "
    "<code>services/dashboard/dashboard/frontend</code>, or use the production image.</p>"
)


@app.get("/", include_in_schema=False)
async def _app_shell():
    """Serve the SPA app shell with revalidation headers. Registered before the catch-all mount
    so it wins for the exact `/` path; hashed assets are still served by the mount below."""
    index = frontend_dist / "index.html"
    if not index.exists():
        return HTMLResponse(_UNBUILT_SHELL, headers={"Cache-Control": "no-cache"})
    return FileResponse(str(index), media_type="text/html", headers={"Cache-Control": "no-cache"})


if (frontend_dist / "index.html").exists():
    app.mount("/", _NoCacheHTMLStaticFiles(directory=str(frontend_dist), html=True), name="static")
