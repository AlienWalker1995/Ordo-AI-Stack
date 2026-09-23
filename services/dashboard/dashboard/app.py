"""Ordo AI Stack Dashboard — unified model management and service hub."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Lock for shared mutable state accessed from both async handlers and background threads
_state_lock = threading.Lock()

import psutil
import yaml

logger = logging.getLogger(__name__)

import httpx as _httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from dashboard import gpu_stats, settings
from dashboard.routes_console import router as console_router
from dashboard.routes_hub import router as hub_router
from dashboard.routes_orchestration import router as orchestration_router
from dashboard.services_catalog import OPS_SERVICE_MAP
from dashboard.settings import AUTH_REQUIRED as _AUTH_REQUIRED
from dashboard.settings import DASHBOARD_AUTH_TOKEN

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
app.include_router(console_router)
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


def _model_gateway_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if MODEL_GATEWAY_API_KEY:
        headers["Authorization"] = f"Bearer {MODEL_GATEWAY_API_KEY}"
    return headers

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


# ── MCP (LiteLLM's MCP gateway on model-gateway) ──────────────────────────────────────────────────
# The enabled server set is RENDER-OWNED: `ordo render` emits out/mcp/servers.json (mounted read-only
# at /mcp-config) from the enabled kind=mcp plugins in out/ordo.yaml. Health comes from LiteLLM
# (/v1/mcp/server/health + the per-server outcomes tools/list returns), never inferred. A UI toggle
# edits ordo.yaml's `plugins:` list (the single source of truth) and tells the operator to re-render
# and recreate model-gateway: config-file MCP servers reload only on restart (no hot reload).
# MODEL_GATEWAY_URL / MODEL_GATEWAY_API_KEY are the module-level constants defined near the top.
MCP_SERVERS_PATH = os.environ.get("MCP_SERVERS_PATH")
ORDO_SOURCE_PATH = os.environ.get("ORDO_SOURCE_PATH")
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "x-litellm-api-key": f"Bearer {MODEL_GATEWAY_API_KEY}",
}
MCP_APPLY_HINT = ("saved to ordo.yaml; apply with an `ordo render` "
                  "(`ordo --source out/ordo.yaml render --out out`) and a model-gateway recreate "
                  "(LiteLLM reads config-file MCP servers at startup only)")


def _read_servers_json() -> dict:
    """The rendered out/mcp/servers.json: {servers: [...], plugin_map: {server_id: plugin_id}}.
    Empty structure when the mount is absent (an older render) so callers degrade, not crash."""
    if not MCP_SERVERS_PATH:
        return {"servers": [], "plugin_map": {}}
    p = Path(MCP_SERVERS_PATH)
    if not p.exists():
        return {"servers": [], "plugin_map": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("servers.json read failed: %s", e)
        return {"servers": [], "plugin_map": {}}
    servers = data.get("servers") if isinstance(data.get("servers"), list) else []
    plugin_map = data.get("plugin_map") if isinstance(data.get("plugin_map"), dict) else {}
    return {"servers": servers, "plugin_map": {str(k): str(v) for k, v in plugin_map.items()}}


def _read_mcp_servers() -> list[str]:
    """Enabled server ids, in render order."""
    return [str(s["id"]) for s in _read_servers_json()["servers"] if s.get("id")]


def _read_mcp_server_names() -> list[tuple[str, str]]:
    """(server_id, litellm_name) for each enabled server, in render order.

    LiteLLM cannot hold a `-` in a server name (it prefixes tools as `<name>-<tool>`), so render
    derives a hyphen-free `litellm_name` and every LiteLLM-side lookup keys on that, while the UI
    row keeps the hyphenated server id. An older render has no `litellm_name` key: derive the same
    way render does rather than silently reporting the server unknown."""
    names = []
    for s in _read_servers_json()["servers"]:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        names.append((sid, str(s.get("litellm_name") or sid.replace("-", "_"))))
    return names


def _read_server_plugin_map() -> dict[str, str]:
    """server_id -> plugin_id for EVERY registered kind=mcp plugin (enabled + available-but-disabled)."""
    return _read_servers_json()["plugin_map"]


def _parse_sse_json(text: str) -> list[dict]:
    """LiteLLM answers /mcp with text/event-stream frames even for one JSON-RPC call; accept both."""
    msgs: list[dict] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                msgs.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    if not msgs and text.strip():
        try:
            msgs.append(json.loads(text))
        except json.JSONDecodeError:
            pass
    return msgs


def _mcp_rows(payload: object) -> list[dict]:
    """LiteLLM returns a bare list here, but tolerate the {"servers"|"data": [...]} envelope."""
    if isinstance(payload, dict):
        payload = payload.get("servers") or payload.get("data") or []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _join_mcp_health(servers_rows: list[dict], health_rows: list[dict]) -> dict[str, str]:
    """{litellm server name: healthy|unhealthy|unknown}, joining the two LiteLLM MCP endpoints.

    GET /v1/mcp/server/health (LiteLLM 1.100.1) reports only a HASHED `server_id` and a status; the
    human-readable name lives on GET /v1/mcp/server as `server_name` (or `alias` when one is set).
    So the health rows are joined onto the server rows by `server_id`. A health row whose id has no
    server row is dropped: callers look the status up by name, never by hash.
    """
    names = {}
    for row in servers_rows:
        server_id = row.get("server_id")
        name = row.get("server_name") or row.get("alias")
        if server_id and name:
            names[str(server_id)] = str(name)
    health = {}
    for row in health_rows:
        name = names.get(str(row.get("server_id")))
        if name:
            health[name] = str(row.get("status", "unknown"))
    return health


async def _litellm_mcp_health() -> dict[str, str]:
    """{litellm server name: healthy|unhealthy|unknown} from LiteLLM's MCP endpoints.
    Keyed by the hyphen-free LiteLLM names, NOT our hyphenated server ids."""
    try:
        client = _get_http_client()
        headers = {"Authorization": f"Bearer {MODEL_GATEWAY_API_KEY}"}
        servers_r, health_r = await asyncio.gather(
            client.get(f"{MODEL_GATEWAY_URL}/v1/mcp/server", headers=headers, timeout=20.0),
            client.get(f"{MODEL_GATEWAY_URL}/v1/mcp/server/health", headers=headers, timeout=20.0))
        if servers_r.status_code != 200 or health_r.status_code != 200:
            logger.debug("mcp server health HTTP %s (servers) / %s (health)",
                         servers_r.status_code, health_r.status_code)
            return {}
        return _join_mcp_health(_mcp_rows(servers_r.json()), _mcp_rows(health_r.json()))
    except Exception as e:  # noqa: BLE001 - degrade to unknown, never 500 the tab
        logger.debug("mcp server health failed: %s", e)
        return {}


async def _litellm_mcp_outcomes() -> tuple[bool, dict[str, dict], str | None]:
    """(gateway_ok, {litellm server name: {status, tool_count}}, error) via tools/list on /mcp. The
    keys are LiteLLM's own (hyphen-free) server names. LiteLLM puts
    per-server outcomes in result._meta['litellm.ai/server_outcomes']; a down upstream is
    `unreachable` there while the endpoint itself stays 200 (alert on outcomes, not status codes)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        r = await _get_http_client().post(f"{MODEL_GATEWAY_URL}/mcp", json=body, headers=_MCP_HEADERS, timeout=60.0)
    except Exception as e:  # noqa: BLE001
        return False, {}, str(e)
    if r.status_code >= 400:
        return False, {}, f"HTTP {r.status_code}"
    for m in _parse_sse_json(r.text):
        if "result" in m:
            result = m["result"] or {}
            outcomes = (result.get("_meta") or {}).get("litellm.ai/server_outcomes") or {}
            return True, {str(k): dict(v) for k, v in outcomes.items()}, None
    return False, {}, "tools/list returned no result"


# ── Persistence: an MCP toggle must update ordo.yaml's `plugins:` list ────────────────────────────
# The enabled server set is RENDER-OWNED: a re-render reseeds out/mcp/servers.json from the enabled
# kind=mcp plugins in out/ordo.yaml, and LiteLLM reads its MCP servers from the rendered fragment at
# startup. So the ONLY durable place a toggle can land is that source. We translate the toggled
# server_id -> its plugin_id (via servers.json's plugin_map) and surgically add/remove that
# `  - <plugin>` line in ordo.yaml, preserving every other line + comment. ordo.yaml stays the single
# source of truth: the next `ordo render` regenerates the SAME servers.json, so there is no drift.

# A `- <plugin-id>` item in a block-style YAML list (optional indent, dash, id, optional trailing
# comment). Zero-indent items are what `yaml.safe_dump` emits (a wizard-written source), so accept
# them too. MIRRORS ordo/source_edit.PLUGIN_ITEM_RE — keep the two in sync (the canonical is in the
# ordo package; the dashboard image doesn't vendor `ordo`, so this is a validated copy).
_PLUGIN_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s+(?P<id>[A-Za-z0-9._-]+)\s*(?:#.*)?$")


def _ordo_source_path() -> Path | None:
    """Path to the mounted, writable ordo.yaml source, or None (a toggle then cannot be persisted)."""
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
    change. The caller catches this and surfaces a 'not persistent' note rather than risking the
    operator's hand-authored source.
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
    plugins list. Never raises, it returns a status the endpoint attaches to its response:

      {persistent: bool, plugin: str|None, note: str|None}

    Not persistent when: ordo.yaml isn't mounted/writable; the server isn't a registered mcp plugin
    (adding a brand-new non-plugin MCP to ordo.yaml is OUT OF SCOPE, flagged rather than faked); or a
    safe surgical edit can't be guaranteed. There is no live path any more, so in every such case
    nothing changed at all, which the note states.
    """
    path = _ordo_source_path()
    if not path:
        return {"persistent": False, "plugin": None,
                "note": "ordo.yaml not mounted (ORDO_SOURCE_PATH unset) - change not persisted; "
                        "ordo.yaml left untouched."}
    plugin = _read_server_plugin_map().get(server)
    if not plugin:
        return {"persistent": False, "plugin": None,
                "note": f"'{server}' is not a registered mcp plugin - change not persisted; ordo.yaml "
                        "left untouched. Adding a brand-new non-plugin MCP to ordo.yaml is out of "
                        "scope."}
    try:
        original = path.read_text(encoding="utf-8")
        edited = _edit_plugins_list(original, plugin, action)
        if edited != original:
            # In-place write (NOT write-temp-then-rename): ordo.yaml is a SINGLE-FILE bind mount,
            # so the app user can neither create a sibling `.tmp` (its dir is the read-only container
            # root) nor rename over the mount. `edited` is already validated inside _edit_plugins_list
            # (round-trips through yaml.safe_load + asserts the exact plugins-set change), so writing
            # the known-good content directly is safe.
            path.write_text(edited, encoding="utf-8")
        return {"persistent": True, "plugin": plugin, "note": None}
    except (ValueError, OSError) as e:
        logger.warning("ordo.yaml persist failed for server=%s plugin=%s action=%s: %s",
                       server, plugin, action, e)
        return {"persistent": False, "plugin": plugin,
                "note": f"could not safely edit ordo.yaml ({e}) - change not persisted; ordo.yaml "
                        "left untouched."}


@app.get("/api/mcp/servers")
async def mcp_servers():
    """Enabled MCP servers (rendered set) + every registered server id the toggle can enable."""
    data = _read_servers_json()
    enabled = [str(s["id"]) for s in data["servers"] if s.get("id")]
    return {
        "enabled": enabled,
        "configured": sorted(data["plugin_map"]),
        "dynamic": _ordo_source_path() is not None,
        "registry": {"servers": {str(s["id"]): s for s in data["servers"] if s.get("id")}},
        "ok": True,
    }


@app.get("/api/mcp/health")
async def mcp_health():
    """Gateway + per-server health from LiteLLM. A server is ok ONLY when its health probe is
    `healthy` AND tools/list reports it `ok` with tools; nothing falls back to gateway-level status."""
    enabled = _read_mcp_server_names()
    health, (gateway_ok, outcomes, gateway_error) = await asyncio.gather(_litellm_mcp_health(),
                                                                        _litellm_mcp_outcomes())
    servers = []
    for sid, litellm_name in enabled:
        # LiteLLM reports under its hyphen-free name; the row we return keeps the hyphenated id
        status = health.get(litellm_name, "unknown")
        outcome = outcomes.get(litellm_name, {})
        tool_count = int(outcome.get("tool_count") or 0)
        ok = gateway_ok and status == "healthy" and outcome.get("status") == "ok" and tool_count > 0
        if ok:
            err = None
        elif not gateway_ok:
            err = gateway_error or "gateway unreachable"
        else:
            err = f"health={status}, tools/list={outcome.get('status', 'no outcome')}, tools={tool_count}"
        servers.append({"id": sid, "ok": ok, "status": status, "error": err, "tool_count": tool_count})
    return {
        "ok": gateway_ok,
        "gateway": "reachable" if gateway_ok else "unreachable",
        "gateway_error": None if gateway_ok else gateway_error,
        "servers": servers,
    }


class McpAddRequest(BaseModel):
    server: str


class McpRemoveRequest(BaseModel):
    server: str


def _valid_mcp_server_name(name: str) -> bool:
    if not name or len(name) > 100:
        return False
    return all(c.isalnum() or c in "-_." for c in name)


@app.post("/api/mcp/add")
async def mcp_add(req: McpAddRequest):
    """Enable a REGISTERED MCP plugin: persists to ordo.yaml; applied on the next render + recreate."""
    server = req.server.strip()
    if not _valid_mcp_server_name(server):
        raise HTTPException(status_code=400, detail="Invalid server id.")
    data = _read_servers_json()
    if server not in data["plugin_map"]:
        raise HTTPException(status_code=400, detail=(
            f"'{server}' is not a registered MCP plugin. New servers are added as services/<id>/plugin.yaml "
            "(kind: mcp), not from a catalog."))
    enabled = [str(s["id"]) for s in data["servers"] if s.get("id")]
    if server in enabled:
        return {"status": "already_enabled", "servers": enabled, "applied": False, "next": None}
    persist = _persist_mcp_toggle(server, "add")
    logger.info("MCP_SERVER_ADDED server=%s persistent=%s plugin=%s", server, persist["persistent"], persist["plugin"])
    return {"status": "added", "servers": enabled + [server], "applied": False, "next": MCP_APPLY_HINT, **persist}


@app.post("/api/mcp/remove")
async def mcp_remove(req: McpRemoveRequest):
    """Disable an enabled MCP plugin: persists to ordo.yaml; applied on the next render + recreate."""
    server = req.server.strip()
    if not _valid_mcp_server_name(server):
        raise HTTPException(status_code=400, detail="Invalid server id.")
    enabled = _read_mcp_servers()
    if server not in enabled:
        return {"status": "already_removed", "servers": enabled, "applied": False, "next": None}
    persist = _persist_mcp_toggle(server, "remove")
    logger.info("MCP_SERVER_REMOVED server=%s persistent=%s plugin=%s", server, persist["persistent"], persist["plugin"])
    return {"status": "removed", "servers": [s for s in enabled if s != server], "applied": False,
            "next": MCP_APPLY_HINT, **persist}


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
    return name.removesuffix(".gguf")


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
    """Per-service compute pressure (CPU/RAM/VRAM)."""
    from dashboard.services_catalog import OPS_SERVICE_MAP, SERVICES

    ops_url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000").rstrip("/")
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    host_info = {
        "cpu_cores": psutil.cpu_count() or 0,
        "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
    }

    def _unavailable_payload():
        services_out = [{
            "id": s["id"], "name": s["name"],
            "cpu_pct": None, "mem_gb": None, "mem_pct": None,
            "vram_gb": None, "vram_pct": None,
            "has_gpu": bool(s.get("has_gpu", False)),
            "running": None,
        } for s in SERVICES]
        return {"gpu": None, "host": host_info, "services": services_out, "vram_aggregate_unavailable": True, "unavailable": True}

    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{ops_url}/stats/services", headers=headers)
            if r.status_code != 200:
                return _unavailable_payload()
            raw = r.json()
    except (_httpx.RequestError, OSError) as e:
        logger.debug("service-pressure: ops-controller unreachable: %s", e)
        return _unavailable_payload()

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
