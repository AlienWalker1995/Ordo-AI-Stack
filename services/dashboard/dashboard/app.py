"""Ordo AI Stack Dashboard — unified model management and service hub."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

# Lock for shared mutable state accessed from both async handlers and background threads
_state_lock = threading.Lock()

import psutil

logger = logging.getLogger(__name__)

# uvicorn configures its own loggers only, so the root logger has no handler and this app's INFO
# and DEBUG records are dropped. Give the root a WARNING handler once: dependency failures log
# through it (dashboard/diagnostics.py).
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")

import httpx as _httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.middleware.gzip import GZipMiddleware

from dashboard import auth, diagnostics, live_work
from dashboard.console import CHAT_MODEL_SUFFIXES
from dashboard.routes_auth import router as auth_router
from dashboard.routes_console import LLAMACPP_CPU_URL
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
    # An exception unwinds past the trace middleware (which has reset its ContextVar by then), so
    # the id comes from request.state; the 500 stamps its own header and body.
    trace = getattr(request.state, "trace_id", None) or diagnostics.current_trace_id()
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "trace_id": trace},
                        headers={"X-Trace-Id": trace})


@app.exception_handler(diagnostics.DependencyHTTPException)
async def _dependency_error_handler(request: Request, exc: diagnostics.DependencyHTTPException):
    """An HTTP error a recorded dependency failure caused: detail as usual, plus its DEP code."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code},
                        headers=exc.headers)


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
    # /api/throughput/record and /api/work/events: require THROUGHPUT_RECORD_TOKEN when set
    # (model-gateway internal; PRD §3.E)
    if path in ("/api/throughput/record", "/api/work/events"):
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


# A sane inbound X-Request-ID is reused as the trace id so an operator can correlate their own
# calls; anything else gets a fresh one. It is correlation metadata, not authentication: any
# client may set it (spoofable/collidable) — the regex only bounds length and charset against
# log injection.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """Mint (or reuse) the request's trace id and stamp X-Trace-Id on every response.

    Defined after auth_middleware in source order, so it wraps OUTSIDE it (the decorators wrap in
    reverse): 401s from the auth middleware carry the header too. The global 500 handler stamps
    its own, because an exception unwinds past this middleware.
    """
    inbound = request.headers.get("X-Request-ID", "").strip()
    trace = inbound if _REQUEST_ID_RE.fullmatch(inbound) else diagnostics.new_trace_id()
    # request.state outlives the ContextVar: an exception unwinds past this middleware (which has
    # reset the var by then), and the global 500 handler reads the id back from here.
    request.state.trace_id = trace
    token = diagnostics.set_trace_id(trace)
    try:
        response = await call_next(request)
    finally:
        diagnostics.reset_trace_id(token)
    response.headers["X-Trace-Id"] = trace
    return response


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

_throughput_samples: dict[str, list[dict]] = {}   # {model file: [{"tps", "ts"}]}
_ttft_samples: dict[str, list[dict]] = {}         # {model file: [{"ms", "ts"}]}
# The same samples grouped by ROLE: which server answered, independent of the file name. A model
# swap changes the file, not the role, so the Overview medians (computed from these series)
# survive it instead of restarting from zero samples under the new name.
_role_samples: dict[str, list[dict]] = {}         # {role: [{"tps", "ts", "model"}]}
_ROLES = ("gpu-chat", "cpu-fallback", "embed")
# The api_base host that served a completion names its service (the gateway sends it per sample);
# the render's /model-config maps each loaded file to the same service ids.
_ROLE_BY_SERVICE = {"inference-engine-gpu": "gpu-chat", "inference-engine-cpu": "cpu-fallback", "inference-engine-embed": "embed"}
_MAX_SAMPLES_PER_MODEL = 500
_MAX_TRACKED_MODELS = 50
# v2: samples are timestamped dicts. v1 stored bare floats keyed largely by routing
# ALIAS (one `local-chat` bucket conflating every model ever active behind it, CPU
# failover included) — unattributable, so version bumps trigger a clean reset.
# v3: tok/s is the engine's generation speed. v2 divided by the whole request, queue and prompt
# reading included, so a 206 tok/s GPU read as 17 tok/s while requests queued.
# v4: samples are also stored per role (above), so a model swap does not reset the medians.
# v3 files migrate on first use: their per-file series are assigned to the role the render served
# them under, then the store is rewritten as v4.
_THROUGHPUT_STORE_VERSION = 4
_migration_pending = False
_SAMPLE_MAX_AGE_SEC = 7 * 86400  # models with no sample in 7 days leave the store


def _evict_stale_models(now: float) -> None:
    """Drop models whose newest sample is older than _SAMPLE_MAX_AGE_SEC, and prune the role
    series to the same window. Call while holding _state_lock."""
    cutoff = now - _SAMPLE_MAX_AGE_SEC
    for store in (_throughput_samples, _ttft_samples):
        stale = [m for m, s in store.items() if not s or s[-1]["ts"] < cutoff]
        for m in stale:
            del store[m]
    for role, series in list(_role_samples.items()):
        fresh = [s for s in series if s["ts"] >= cutoff]
        if fresh:
            _role_samples[role] = fresh
        else:
            del _role_samples[role]

# Last benchmark result (persists across page refresh until dashboard restart)
_last_benchmark: dict | None = None

DASHBOARD_DATA_PATH = Path(os.environ.get("DASHBOARD_DATA_PATH", "./data/dashboard")).resolve()
DASHBOARD_DATA_PATH.mkdir(parents=True, exist_ok=True)
_THROUGHPUT_FILE = DASHBOARD_DATA_PATH / "throughput.json"


def _load_throughput_state() -> None:
    """Load throughput samples and last benchmark from disk (R4). v1 files (no
    version field) get a clean reset — their samples are un-timestamped and
    alias-conflated; only last_benchmark carries over. A v3 file loads its per-file
    series as-is and stays marked for migration: _migrate_throughput_store assigns
    them to roles once the render's /model-config can be read, then rewrites it as v4."""
    global _throughput_samples, _ttft_samples, _last_benchmark, _migration_pending, _role_samples
    if not _THROUGHPUT_FILE.exists():
        return
    try:
        data = json.loads(_THROUGHPUT_FILE.read_text(encoding="utf-8"))
        _last_benchmark = data.get("last_benchmark") if isinstance(data.get("last_benchmark"), dict) else None
        version = data.get("version")
        if version == _THROUGHPUT_STORE_VERSION - 1:
            _throughput_samples = {
                k: [s for s in v if isinstance(s, dict) and "tps" in s and "ts" in s]
                for k, v in (data.get("samples") or {}).items() if isinstance(v, list)
            }
            _ttft_samples = {
                k: [s for s in v if isinstance(s, dict) and "ms" in s and "ts" in s]
                for k, v in (data.get("ttft_samples") or {}).items() if isinstance(v, list)
            }
            _migration_pending = True
            logger.info("Throughput store is v3: migrating its per-file series to roles on first use")
            return
        if version != _THROUGHPUT_STORE_VERSION:
            logger.warning(
                "Throughput store is v%s (want v%s) — resetting samples, keeping last_benchmark",
                version if version is not None else 1, _THROUGHPUT_STORE_VERSION,
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
        _role_samples = {
            r: [s for s in v if isinstance(s, dict) and "tps" in s and "ts" in s]
            for r, v in (data.get("role_samples") or {}).items() if isinstance(v, list)
        }
    except Exception as e:
        logger.warning("Throughput state load failed: %s", e)


def _save_throughput_state() -> None:
    """Persist throughput state to disk via atomic write-then-rename. While the v3 -> v4
    migration is pending the file stays v3-shaped: a restart must reload the per-file series
    and retry the migration, not read a v4 file whose role_samples the v3 load never populated
    and drop the migration forever."""
    try:
        _THROUGHPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _THROUGHPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "version": _THROUGHPUT_STORE_VERSION - 1 if _migration_pending else _THROUGHPUT_STORE_VERSION,
            "samples": _throughput_samples,
            "role_samples": _role_samples,
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

_migration_lock = asyncio.Lock()
# The v3 -> v4 migration needs the render's /model-config, which can be unreachable for minutes
# mid-deploy, and the gateway posts a record after every completion. So at most ONE caller pays
# for an attempt (bounded by the 5 s read) per cooldown window; every other caller skips without
# waiting. While the migration waits, the Overview answers from the per-file series alone: .ninfer
# and embedding files classify by name, chat files wait for the config.
_MIGRATION_RETRY_SECONDS = 30.0
_migration_in_progress = False
_migration_next_try = 0.0


def _classify_file(file: str, model_config: dict | None) -> str | None:
    """The role one model file served in: the render's own mapping first (which service loads
    it now, plus the catalog the GPU chat server may serve), then the file name; None when
    neither can tell (see the last branch)."""
    if model_config:
        for entry in model_config.get("model_files") or []:
            if isinstance(entry, dict) and entry.get("file") == file:
                return _ROLE_BY_SERVICE.get(str(entry.get("service")), "gpu-chat")
        if file == model_config.get("active_file"):
            return "gpu-chat"
        for m in model_config.get("available") or []:
            if isinstance(m, dict) and m.get("file") == file:
                return "gpu-chat"
    if (file or "").lower().endswith(".ninfer"):
        return "gpu-chat"
    if _is_embedding_model(file):
        return "embed"
    # A GGUF the render no longer names could have served on either chat engine (a GPU model swapped
    # out before the migration looks exactly like a departed CPU model). Guessing would put GPU-speed
    # samples under the CPU median for up to 7 days, so such a file keeps its per-file series only.
    return None


def _merge_into_roles(roles: dict[str, list[dict]], samples: dict[str, list[dict]],
                      model_config: dict | None) -> None:
    """Assign every per-file series to a role and merge it into the role series, sorted by time."""
    for file, series in samples.items():
        role = _classify_file(file, model_config)
        if role is None:
            continue
        bucket = roles.setdefault(role, [])
        for s in series:
            bucket.append({"tps": s["tps"], "ts": s["ts"], "model": file})
    for bucket in roles.values():
        bucket.sort(key=lambda s: s["ts"])


async def _migrate_throughput_store() -> None:
    """One-shot v3 -> v4: assign the per-file series to the roles the render served them under,
    then persist. The render's /model-config names the service that loads each file; while it
    cannot be read the store stays v3-shaped in memory AND on disk (saves keep writing version 3)
    and only .ninfer and embedding files classify by name, so a misclassification is never
    persisted. The role series is REBUILT from
    the per-file series rather than merged into the live one: every sample already sits in the
    per-file series (records made while the migration waited included), so merging into the live
    buckets would count the pending ones twice; rebuilding also trims each bucket to the cap."""
    global _migration_pending, _role_samples, _migration_next_try
    async with _migration_lock:
        if not _migration_pending:
            return
        code, data = await _ops_request("GET", "/model-config", timeout=5.0)
        config = data if code == 200 and isinstance(data, dict) else None
        if config is None:
            _migration_next_try = time.monotonic() + _MIGRATION_RETRY_SECONDS
            return
        with _state_lock:
            migrated: dict[str, list[dict]] = {}
            _merge_into_roles(migrated, _throughput_samples, config)
            for role in migrated:
                if len(migrated[role]) > _MAX_SAMPLES_PER_MODEL:
                    migrated[role] = migrated[role][-_MAX_SAMPLES_PER_MODEL:]
            _role_samples = migrated
            _migration_pending = False
            _save_throughput_state()


async def _maybe_migrate_throughput_store() -> None:
    """Run one migration attempt unless one is in flight or a failed one is cooling down. A caller
    that skips answers from the per-file series; nothing ever queues behind the attempt."""
    global _migration_in_progress
    if not _migration_pending or _migration_in_progress or time.monotonic() < _migration_next_try:
        return
    _migration_in_progress = True
    try:
        await _migrate_throughput_store()
    finally:
        _migration_in_progress = False


def _role_for(model: str, alias: str, backend: str) -> str:
    """The role of one incoming sample: which server answered, from the payload the gateway
    already sends (backend is the api_base host that served it). A gateway old enough to send
    neither falls back to the alias, then the file name."""
    role = _ROLE_BY_SERVICE.get((backend or "").strip().lower())
    if role:
        return role
    alias = (alias or "").strip().lower()
    if alias == "local-embed" or _is_embedding_model(model):
        return "embed"
    if alias == "local-chat-cpu":
        return "cpu-fallback"
    return "gpu-chat"


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
    await _maybe_migrate_throughput_store()
    now = time.time()
    role = _role_for(model, req.alias, req.backend)
    with _state_lock:
        _evict_stale_models(now)
        if model not in _throughput_samples:
            if len(_throughput_samples) >= _MAX_TRACKED_MODELS:
                return {"ok": True}
            _throughput_samples[model] = []
        _throughput_samples[model].append({"tps": req.output_tokens_per_sec, "ts": now})
        if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
            _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        # While the v3 migration waits for /model-config the per-file series is the single source
        # of truth: this sample reaches the role series exactly once, when the migration rebuilds
        # it. Appending here as well would count it twice after the merge (and in the live roles
        # stats, which merges the per-file series on the fly while pending).
        if not _migration_pending:
            bucket = _role_samples.setdefault(role, [])
            bucket.append({"tps": req.output_tokens_per_sec, "ts": now, "model": model})
            if len(bucket) > _MAX_SAMPLES_PER_MODEL:
                _role_samples[role] = bucket[-_MAX_SAMPLES_PER_MODEL:]
        if req.ttft_ms > 0 and (model in _ttft_samples or len(_ttft_samples) < _MAX_TRACKED_MODELS):
            if model not in _ttft_samples:
                _ttft_samples[model] = []
            _ttft_samples[model].append({"ms": req.ttft_ms, "ts": now})
            if len(_ttft_samples[model]) > _MAX_SAMPLES_PER_MODEL:
                _ttft_samples[model] = _ttft_samples[model][-_MAX_SAMPLES_PER_MODEL:]
        _maybe_save_throughput()
    return {"ok": True}


# --- Live model work (dashboard/live_work.py). In memory only: a restart starts empty. ---

_work_store = live_work.WorkStore()
_CPU_SLOTS_TIMEOUT = 2.0
_CPU_SLOTS_CACHE_TTL = 2.0
_cpu_slots_cache: dict = {"checked": 0.0, "value": None}


class WorkEventRequest(BaseModel):
    event: Literal["started", "ended"]
    call_id: str = Field(min_length=1, max_length=128)
    caller: str = Field(default="", max_length=64)
    model_group: str = Field(default="", max_length=256)
    model: str = Field(default="", max_length=256)
    deployment: str = Field(default="", max_length=64)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    outcome: Literal["ok", "error"] = "ok"
    error: str | None = Field(default=None, max_length=128)
    started_at: float | None = Field(default=None, ge=0)
    ended_at: float | None = Field(default=None, ge=0)
    fallback: StrictBool = False


@app.post("/api/work/events")
async def work_events(req: WorkEventRequest):
    """A model gateway request started or ended (throughput_callback.py). Fire-and-forget."""
    event = req.model_dump()
    if req.event == "started":
        _work_store.started(event, time.time())
    else:
        _work_store.ended(event, time.time())
    return {"ok": True}


async def _cpu_prefill() -> dict:
    """The CPU engine's prefill progress from llama.cpp GET /slots, cached for 2 s. The GPU engine
    (NInfer) serves no /slots, so its in-flight work comes from the gateway's events alone."""
    now = time.monotonic()
    if _cpu_slots_cache["value"] is not None and now - _cpu_slots_cache["checked"] < _CPU_SLOTS_CACHE_TTL:
        return _cpu_slots_cache["value"]
    progress = None
    try:
        response = await _get_http_client().get(f"{LLAMACPP_CPU_URL}/slots", timeout=_CPU_SLOTS_TIMEOUT)
        response.raise_for_status()
        progress = live_work.slots_progress(response.json())
    except Exception as exc:
        logger.debug("CPU engine /slots unavailable: %s", exc)
    if progress is None:
        value = {"available": False, "processing": False, "prompt_tokens": 0, "processed": 0, "cached": 0}
    else:
        value = {"available": True, **progress}
    _cpu_slots_cache.update(checked=now, value=value)
    return value


@app.get("/api/work")
async def work():
    """Requests in flight on the local engines and, per caller, the last five minutes."""
    body = _work_store.snapshot(time.time())
    for engine in body["engines"].values():
        engine["prefill"] = None
    cpu = body["engines"]["inference-engine-cpu"]
    # Read the CPU engine only while the fallback is serving something.
    if cpu["inflight"]:
        cpu["prefill"] = await _cpu_prefill()
    return body


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
    """Per-model AND per-role throughput stats over timestamped samples: peak, p50/p95/p99,
    latest, sample_count, first_ts/last_ts. Includes last_benchmark if available."""
    await _maybe_migrate_throughput_store()
    result: dict[str, dict] = {}
    now = time.time()
    with _state_lock:
        _evict_stale_models(now)
        snapshot = {m: list(s) for m, s in _throughput_samples.items()}
        ttft_snapshot = {m: list(s) for m, s in _ttft_samples.items()}
        role_snapshot = {r: list(s) for r, s in _role_samples.items()}
        pending = _migration_pending
        benchmark = dict(_last_benchmark) if _last_benchmark else None
    # While the v3 migration waits for /model-config, the role series is computed on the fly from
    # the per-file series: .ninfer and embedding files classify by name, plain chat GGUFs wait
    # for the config, so the Overview's chat medians stay empty unless a .ninfer file is present.
    if pending:
        _merge_into_roles(role_snapshot, snapshot, None)
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
    roles_out = {}
    for role in _ROLES:
        series = role_snapshot.get(role) or []
        tps_vals = [s["tps"] for s in series]
        roles_out[role] = {
            "p50": round(_percentile(sorted(tps_vals), 50), 1) if tps_vals else None,
            "n": len(series),
            "first_ts": series[0]["ts"] if series else None,
            "last_ts": series[-1]["ts"] if series else None,
            "model": series[-1].get("model") if series else None,
        }
    active = await _throughput_active_model()
    out: dict = {
        "models": result,
        "roles": roles_out,
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
    started = time.perf_counter()
    try:
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
        if r.status_code < 400:
            try:
                data = r.json()
            except (ValueError, UnicodeDecodeError):
                code = diagnostics.record_failure("GATEWAY", "BADPAYLOAD", "unparseable JSON response",
                                                  target="/v1/chat/completions", elapsed_ms=elapsed_ms)
                raise diagnostics.DependencyHTTPException(502, "Model gateway returned invalid JSON", code)
            diagnostics.mark_ok("GATEWAY")
        elif r.status_code == 400:
            # A 400 means the gateway answered (a bad model name, for instance): mark it ok and
            # pass the gateway's own message through.
            diagnostics.mark_ok("GATEWAY")
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
        else:
            kind = diagnostics.classify_failure(status=r.status_code)
            detail = f"Model gateway answered HTTP {r.status_code}"
            if kind == "HTTP4XX":
                diagnostics.mark_ok("GATEWAY")
                raise HTTPException(status_code=502, detail=detail)
            code = diagnostics.record_failure("GATEWAY", kind, f"HTTP {r.status_code}",
                                              target="/v1/chat/completions", elapsed_ms=elapsed_ms)
            raise diagnostics.DependencyHTTPException(502, detail, code)
    except HTTPException:
        raise
    except Exception as e:
        elapsed_ms = (time.perf_counter() - started) * 1000
        code = diagnostics.record_failure("GATEWAY", diagnostics.classify_failure(e),
                                          f"{type(e).__name__}: {e}", target="/v1/chat/completions",
                                          elapsed_ms=elapsed_ms)
        raise diagnostics.DependencyHTTPException(502, f"Model gateway request failed: {e}", code) from e

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
    *,
    timeout: float = 30.0,
    **kwargs,
) -> tuple[int, dict]:
    """Proxy request to ops controller. Returns (status_code, json_body); see _ops_request_coded."""
    status, data, _code = await _ops_request_coded(method, path, timeout=timeout, **kwargs)
    return status, data


async def _ops_request_coded(
    method: str,
    path: str,
    *,
    timeout: float = 30.0,
    **kwargs,
) -> tuple[int, dict, str | None]:
    """Proxy request to ops controller. Returns (status_code, json_body, dep_code).
    Sends X-Request-ID (the request's trace id, unless a caller overrides it) for audit
    correlation, and records every failure with a DEP-OPS-* code (dashboard/diagnostics.py);
    a success — including a 4xx other than AUTH, which means the controller answered — marks OPS ok.
    dep_code is the DEP code of the failure it recorded, None when it recorded none (_ops_proxy
    passes it to the page).
    """
    if not OPS_CONTROLLER_TOKEN:
        code = diagnostics.record_failure("OPS", "NOTCONFIGURED", "OPS_CONTROLLER_TOKEN not set",
                                          target=path, elapsed_ms=0.0)
        return 503, {"detail": "OPS_CONTROLLER_TOKEN not configured"}, code
    url = f"{OPS_CONTROLLER_URL.rstrip('/')}{path}"
    extra = kwargs.pop("headers", {})
    # The trace id is the default; a caller may override it (the same convention as X-Actor below).
    extra = {"X-Request-ID": diagnostics.current_trace_id(), **extra}
    # X-Actor names the caller in ops-controller's audit log; a caller may override it.
    headers = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}", "X-Actor": "dashboard", **extra}
    started = time.perf_counter()
    try:
        r = await _get_http_client().request(method, url, headers=headers, timeout=timeout, **kwargs)
    except _httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - started) * 1000
        detail = f"ops-controller did not answer {method} {path} within {timeout:g} s"
        if method != "GET":
            detail += "; the action may still be running, so check the service before retrying"
        code = diagnostics.record_failure("OPS", "TIMEOUT", detail, target=path, elapsed_ms=elapsed_ms)
        return 503, {"detail": detail}, code
    except Exception as e:
        elapsed_ms = (time.perf_counter() - started) * 1000
        # The class name plus the message: str(e) alone embeds internal hostnames and ports.
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        code = diagnostics.record_failure("OPS", diagnostics.classify_failure(e), detail,
                                          target=path, elapsed_ms=elapsed_ms)
        # Reuse the sanitized detail in the body too: the raw exception text is exactly what
        # embeds internal hostnames and ports.
        return 503, {"detail": diagnostics.sanitize_detail(detail)}, code
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        data = r.json()
    except (ValueError, UnicodeDecodeError):
        data = {"detail": r.text or "Unknown error"}
        if 400 <= r.status_code < 500 and r.status_code not in (401, 403):
            # A non-AUTH 4xx means ops-controller answered — it is up, so mark ok and pass the
            # body through, matching the parseable path below.
            diagnostics.mark_ok("OPS")
            return r.status_code, data, None
        # An unparseable body is a BADPAYLOAD failure on its own; only an unparseable 5xx/AUTH
        # keeps the status kind.
        kind = "BADPAYLOAD" if r.status_code < 500 and r.status_code not in (401, 403) \
            else diagnostics.classify_failure(status=r.status_code)
        code = diagnostics.record_failure(
            "OPS", kind, f"unparseable JSON body from {method} {path} (HTTP {r.status_code})",
            target=path, elapsed_ms=elapsed_ms)
        return r.status_code, data, code
    code = None
    if r.status_code >= 400:
        kind = diagnostics.classify_failure(status=r.status_code)
        if kind == "HTTP4XX":
            # A 4xx other than AUTH means ops-controller answered — it is up, so mark ok.
            diagnostics.mark_ok("OPS")
        else:
            code = diagnostics.record_failure("OPS", kind, f"HTTP {r.status_code} from {method} {path}",
                                              target=path, elapsed_ms=elapsed_ms)
    else:
        diagnostics.mark_ok("OPS")
    return r.status_code, data, code


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
    """An ops-controller call whose failure reaches the page as an HTTPException with its reason,
    and with the DEP code when _ops_request recorded the failure."""
    status, data, dep_code = await _ops_request_coded(method, path, timeout=timeout, **kwargs)
    if status >= 400:
        detail = _ops_error_detail(status, data)
        if dep_code:
            raise diagnostics.DependencyHTTPException(status, detail, dep_code)
        raise HTTPException(status_code=status, detail=detail)
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


# ops-controller's logs routes take `tail` (1-1000 lines, default 200) and `since` (the previous
# answer's `last_ts`, exclusive) and answer the newest tail with that cursor
# (ordo/control/logs.py); the proxy caps `tail` at the same 1000.
_LOGS_TAIL_MAX = 1000


def _logs_query(tail: str | None, since: str | None) -> str:
    """The query string the logs proxies forward, or a 400 for a tail or since ops would refuse."""
    try:
        lines = 100 if tail is None else int(tail)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"tail must be an integer, got {tail!r}") from None
    if not 1 <= lines <= _LOGS_TAIL_MAX:
        raise HTTPException(status_code=400, detail=f"tail must be between 1 and {_LOGS_TAIL_MAX}, got {lines}")
    params = {"tail": lines}
    if since is not None:
        if not since.strip():
            raise HTTPException(status_code=400, detail="since must not be empty")
        params["since"] = since
    return urlencode(params)


@app.get("/api/ops/services/{service_id}/logs")
async def ops_logs(service_id: str, request: Request, tail: str | None = None, since: str | None = None):
    """Get service logs via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    query = _logs_query(tail, since)
    return await _ops_proxy("GET", f"/services/{ops_id}/logs?{query}", request, timeout=_LOGS_TIMEOUT)


# A container of a managed project (another compose project listed in `managed_projects:`):
# ordo/control/managed_projects.py answers its logs and a confirmed, rate-limited restart.
@app.get("/api/ops/projects/{project}/containers/{name}/logs")
async def ops_project_container_logs(
    project: str, name: str, request: Request, tail: str | None = None, since: str | None = None
):
    """Get a managed-project container's logs via ops controller."""
    query = _logs_query(tail, since)
    path = f"/projects/{quote(project, safe='')}/containers/{quote(name, safe='')}/logs?{query}"
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
    target = f"/collections/{RAG_COLLECTION}"
    started = time.perf_counter()
    try:
        r = await _get_http_client().get(f"{QDRANT_URL}{target}", timeout=5.0)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if r.status_code == 200:
            try:
                info = r.json().get("result", {})
            except (ValueError, UnicodeDecodeError) as e:
                diagnostics.record_failure("QDRANT", "BADPAYLOAD", type(e).__name__, target=target,
                                           elapsed_ms=elapsed_ms)
                return {"ok": False, "error": "Invalid response from Qdrant"}
            diagnostics.mark_ok("QDRANT")
            return {
                "ok": True,
                "collection": RAG_COLLECTION,
                "points_count": info.get("points_count", 0),
                "status": info.get("status", "unknown"),
            }
        if r.status_code == 404:
            diagnostics.mark_ok("QDRANT")
            return {"ok": True, "collection": RAG_COLLECTION, "points_count": 0, "status": "empty"}
        kind = diagnostics.classify_failure(status=r.status_code)
        if kind == "HTTP4XX":
            diagnostics.mark_ok("QDRANT")
        else:
            diagnostics.record_failure("QDRANT", kind, f"HTTP {r.status_code}", target=target,
                                       elapsed_ms=elapsed_ms)
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        elapsed_ms = (time.perf_counter() - started) * 1000
        diagnostics.record_failure("QDRANT", diagnostics.classify_failure(e),
                                   f"{type(e).__name__}: {e}", target=target, elapsed_ms=elapsed_ms)
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
    ops-controller unreachable or answering garbage -> [] (the GPU widgets show none). A 200 whose
    body is not an object records DEP-OPS-BADPAYLOAD (dashboard/diagnostics.py); transport and
    status failures are already recorded by _ops_request."""
    started = time.perf_counter()
    code, data = await _ops_request("GET", "/gpus", timeout=15.0)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if code != 200 or not isinstance(data, dict):
        if code == 200:
            diagnostics.record_failure("OPS", "BADPAYLOAD",
                                       "ops-controller /gpus answered 200 without an object body",
                                       target="/gpus", elapsed_ms=elapsed_ms)
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

    # Through _ops_request (the shared client, X-Actor and X-Request-ID, DEP-OPS-* codes). Its own
    # failures come back as a 503 with a `detail`, which reads as "not answering"; a real answer
    # keeps its status in the reason.
    code, raw = await _ops_request("GET", "/stats/services", timeout=8.0)
    if code != 200:
        detail = raw.get("detail") if isinstance(raw, dict) else None
        if code == 503 and isinstance(detail, str):
            return _unavailable_payload("ops-controller is not answering")
        return _unavailable_payload(f"ops-controller answered HTTP {code}")
    # A 200 whose body did not parse comes back as {"detail": ...} (DEP-OPS-BADPAYLOAD): treat it
    # as unavailable, not as an empty answer.
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), dict):
        return _unavailable_payload("ops-controller returned an unparseable stats payload")

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


# --- Diagnostics ---


@app.get("/api/diagnostics")
async def api_diagnostics() -> dict:
    """Dependency health and recent failures (dashboard/diagnostics.py). Open read like the other
    /api views: per-dependency status, the failure ring newest first, and the code table. Failure
    details are sanitized before they enter the ring (exception class plus a summary, URLs
    stripped), so the open view leaks no internal hostnames or ports."""
    return diagnostics.snapshot()


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
