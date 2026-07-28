"""Single source of truth for "what services exist + how to probe them".

Feeds three surfaces, all derived from the one `SERVICES` catalog:
  * the service grid   — GET /api/services, /api/health (visible_services())
  * ops lifecycle wiring — OPS_SERVICE_MAP
  * the dependency panel — GET /api/dependencies (dependency_services() + probe_all())

The dependency panel used to be a second hardcoded catalog (dependency_registry.json);
it now derives from this one catalog plus INFRA_DEPENDENCIES, so a service's check URL /
name / hint / category lives in exactly one place. Separated from app.py for maintainability.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx as _httpx

logger = logging.getLogger(__name__)

# Dashboard service id -> clean per-service tailnet subdomain label (the tailnet-names
# sidecar plugin serves each UI as https://<label>.<domain>/). Only UI services have a
# sidecar; backend-only services (llamacpp/model-gateway/mcp/qdrant) have no clean name
# and keep their internal URLs. hermes/graph land on their port's root, which 302s to
# the /hermes/ and /codebase-memory/ subpaths, so a bare https://<label>.<domain>/ works.
TAILNET_LABELS = {
    "webui": "chat",
    "comfyui": "comfy",
    "n8n": "n8n",
    "hermes": "hermes",
    "codebase-memory-ui": "graph",
}


def mcp_external_url() -> str | None:
    """The MCP gateway's real external endpoint: the Bearer-gated /mcp route on the
    :443 front door (https://<host>/mcp) — NOT a :8811 port, which isn't published.
    Returns None when the edge host is unknown so the frontend keeps its fallback."""
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    return f"https://{host}/mcp" if host else None


# LiteLLM auto-generates its OpenAPI docs and serves the Swagger UI at its ROOT path
# (`GET model-gateway:11435/` returns the swagger HTML; `/docs` 404s — confirmed by probe).
# The :443 front door's `/llm/*` route strips the `/llm` prefix and proxies to
# model-gateway:11435, so the browsable swagger URL through the edge is
# `https://<host>/llm/` (the stripped prefix maps back onto the service root).
MODEL_GATEWAY_SWAGGER_PATH = "/llm/"


def model_gateway_open_url() -> str | None:
    """Browsable Open link for the model-gateway card: the LiteLLM Swagger UI reached
    through the edge (`https://<host>/llm/`). model-gateway is the one user-facing entry
    in the main grid that has no tailnet sidecar name, so its Open link is built here from
    the edge host. Returns None when the edge host is unknown so the frontend falls back to
    its port/SSO route rather than emitting a broken link."""
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    return f"https://{host}{MODEL_GATEWAY_SWAGGER_PATH}" if host else None


def tailnet_open_url(service_id: str) -> str | None:
    """Clean per-service URL (https://<label>.<domain>/) when the tailnet-names sidecar
    layer is enabled, else None. Both signals come from the rendered env: the enable flag
    the plugin emits (TAILNET_NAMES_ENABLED) and the edge domain (CADDY_TAILNET_DOMAIN).
    Gating on the flag — not merely on the domain being set — keeps the links correct on
    a port-per-service deployment that has the edge but NOT the sidecars."""
    if os.environ.get("TAILNET_NAMES_ENABLED", "").strip().lower() not in ("1", "true"):
        return None
    domain = os.environ.get("CADDY_TAILNET_DOMAIN", "").strip()
    label = TAILNET_LABELS.get(service_id)
    if not domain or not label:
        return None
    return f"https://{label}.{domain}/"

# Map dashboard service id -> ops-controller (compose) service id. Every value here
# MUST be a real compose service name AND be present in ops-api's ALLOWED_SERVICES,
# else the card's start/stop/restart buttons 400. (Locked by test_service_catalog_wiring.)
OPS_SERVICE_MAP = {
    "llamacpp": "llamacpp",
    "model-gateway": "model-gateway",
    "webui": "open-webui",
    "mcp": "mcp-gateway",
    "comfyui": "comfyui",
    "n8n": "n8n",
    "qdrant": "qdrant",
    # Hermes lifecycle buttons target the UI service (hermes-dashboard), NOT the
    # agent/gateway — its self-restart is delicate and it is not allowlisted.
    "hermes": "hermes-dashboard",
    # Voice + background workers (added with their manifest-gated cards below).
    "stt": "stt",
    "tts": "tts",
    "worker": "worker",
    "rag-ingestion": "rag-ingestion",
}

# Each entry's `plugin` names the render plugin (manifest.plugins_enabled id) that gates
# its card. Core services always present in every render carry plugin=None and are never
# gated. visible_services() (below) hides a card only when its plugin is DISABLED, so the
# grid reflects what the render actually enabled. NB: the plugin id is NOT the compose
# profile (e.g. open-webui's profile is `webui` but its plugin id is `open-webui`).
SERVICES = [
    {"id": "llamacpp", "name": "llama.cpp", "port": 8080, "url": "http://localhost:8080", "check": "http://llamacpp:8080/health", "has_gpu": True, "plugin": None, "category": "inference", "background": True,
     "hint": "Backend-only; use model-gateway :11435 from host. Run: docker compose up -d llamacpp"},
    {"id": "model-gateway", "name": "Model Gateway", "port": 11435, "url": "http://localhost:11435", "check": "http://model-gateway:11435/health/liveliness", "has_gpu": False, "plugin": None, "category": "inference",
     "hint": "OpenAI-compatible proxy (LiteLLM). Routes inference to llama.cpp."},
    {"id": "webui", "name": "Open WebUI", "port": 3000, "url": "http://localhost:3000", "check": "http://open-webui:8080", "has_gpu": False, "plugin": "open-webui", "category": "interface",
     "hint": "Uses model-gateway for chat. Check: docker compose logs open-webui"},
    # The MCP gateway answers a bare GET to /mcp with a 4xx (it expects POST/SSE with an
    # Mcp-Session-Id) — `check_4xx_ok` tells the strict dependency probe that a <500 there
    # still means "up". The grid probe (_check_service) already treats <500 as reachable.
    {"id": "mcp", "name": "MCP Gateway", "port": 8811, "url": "http://localhost:8811", "check": "http://mcp-gateway:8811/mcp", "has_gpu": False, "plugin": None, "category": "tools", "check_4xx_ok": True, "background": True,
     "hint": "Add/remove tools from the dashboard. Connect at http://localhost:8811/mcp — see services/mcp-gateway/README.md"},
    {"id": "comfyui", "name": "ComfyUI", "port": 8188, "url": "http://localhost:8188", "check": "http://comfyui:8188", "has_gpu": True, "plugin": "comfyui", "category": "media",
     "hint": "ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU). Run ./compose up -d. Pull LTX-2 via dashboard."},
    {"id": "n8n", "name": "N8N", "port": 5678, "url": "http://localhost:5678", "check": "http://n8n:5678", "has_gpu": False, "plugin": "automation", "category": "automation",
     "hint": "Check: docker compose logs n8n"},
    {"id": "qdrant", "name": "Qdrant", "port": 6333, "url": "http://localhost:6333", "has_gpu": False, "plugin": "rag", "category": "rag", "background": True,
     "check": "http://qdrant:6333/readyz",
     "hint": "Vector DB for RAG. Drop files in data/rag-input/ (with --profile rag) or upload via Open WebUI Documents tab."},
    # Hermes Agent runs as two compose services (hermes-gateway + hermes-dashboard). The dashboard
    # container probes via internal DNS — unhealthy means the Hermes services haven't started.
    {"id": "hermes", "name": "Hermes Agent", "port": 9119, "url": "http://localhost:9119",
     "check": "http://hermes-dashboard:9119/", "has_gpu": False, "plugin": "hermes-dashboard", "category": "agent",
     "hint": "Managed by docker compose. Logs: docker compose logs hermes-dashboard"},
    # Opt-in (--profile codebase-memory). 3D code knowledge-graph visualization, served at
    # https://<host>/codebase-memory/ on its own SSO-gated port :8448 (the codebase-memory-ui
    # container's nginx serves it under that subpath). The "Open" link comes from SSO_ROUTES
    # in the frontend (-> /codebase-memory/), so no `url` is needed. The health check hits the
    # nginx subpath, which proxies through to the UI.
    {"id": "codebase-memory-ui", "name": "Codebase Memory", "port": 9750,
     "check": "http://codebase-memory-ui:9750/codebase-memory/", "has_gpu": False, "plugin": "codebase-memory-ui", "category": "knowledge",
     "hint": "3D code knowledge-graph. Open at https://<host>:8448/codebase-memory/ (Google SSO). "
             "In-memory index — re-index after a restart. Opt-in: --profile codebase-memory"},
    # ── Voice (--profile voice / plugin `voice`) ──────────────────────────────────────────
    # STT + TTS both pin the 1070 (the 5090 lacks the kernels). Check URLs mirror each
    # service's compose healthcheck (confirmed 200 endpoints), probed via internal DNS.
    {"id": "stt", "name": "Speech-to-Text (Whisper)", "port": 8000, "check": "http://stt:8000/v1/models", "has_gpu": True, "plugin": "voice", "category": "voice", "background": True,
     "hint": "faster-whisper-server (OpenAI-compatible). GPU-pinned to the 1070. Opt-in: --profile voice"},
    {"id": "tts", "name": "Text-to-Speech (Kokoro)", "port": 8880, "check": "http://tts:8880/v1/audio/voices", "has_gpu": True, "plugin": "voice", "category": "voice", "background": True,
     "hint": "kokoro-fastapi (OpenAI-compatible). GPU-pinned to the 1070. Opt-in: --profile voice"},
    # ── Headless background workers ───────────────────────────────────────────────────────
    # worker (plugin `worker`, profile media) and rag-ingestion (plugin `rag`) expose NO HTTP
    # port — their only liveness signal is a file-heartbeat container healthcheck, unreachable
    # from this container. So they carry NO `check` (card shows a neutral "unknown" state, not a
    # false-red or a guessed URL) but ARE operator-controllable via the ops-api (start/stop/restart).
    #
    # `background: True` is the marker the frontend uses to move a service OUT of the main
    # user-facing grid into the secondary "Background jobs" section (no "Open" link). Its
    # meaning is "NOT a browsable user-facing UI" — not merely "headless worker". So besides
    # these two portless workers it ALSO tags the infra/backend services that have a port &
    # health check but no browsable UI a person visits: llamacpp, mcp (MCP Gateway), qdrant,
    # stt, tts (see their entries above). The main grid is ONLY the user-facing UIs
    # (webui/comfyui/n8n/hermes/codebase-memory-ui) plus model-gateway (its Open link points
    # at the LiteLLM Swagger UI through the edge; see model_gateway_open_url() below).
    {"id": "worker", "name": "Media Worker", "port": None, "check": None, "has_gpu": False, "plugin": "worker", "category": "media", "background": True,
     "hint": "Headless ComfyUI render worker (no web UI). Health via container healthcheck. Logs: docker compose logs worker"},
    {"id": "rag-ingestion", "name": "RAG Ingestion", "port": None, "check": None, "has_gpu": False, "plugin": "rag", "category": "rag", "background": True,
     "hint": "Headless folder-watch embedder for Qdrant (no web UI). Health via container healthcheck. Logs: docker compose logs rag-ingestion"},
    # Obsidian cross-device notes sync — headless background services (no web UI). CouchDB is the
    # sync server (reached via :443/couchdb); the bridge mirrors it to data/memory-vault/notes/ so
    # the AI reads notes. The optional off-tailnet Funnel (obsidian-livesync-funnel) is a Tailscale
    # config sidecar, NOT a service a person browses — it carries no card (managed via the ops-api).
    {"id": "couchdb", "name": "CouchDB (notes sync)", "port": None, "check": "http://couchdb:5984/_up", "has_gpu": False, "plugin": "obsidian-livesync", "category": "notes", "background": True,
     "hint": "Sync server for Obsidian Self-hosted LiveSync. Reached via :443/couchdb. Logs: docker compose logs couchdb"},
    {"id": "livesync-bridge", "name": "LiveSync Bridge", "port": None, "check": None, "has_gpu": False, "plugin": "obsidian-livesync", "category": "notes", "background": True,
     "hint": "Headless CouchDB <-> data/memory-vault/notes/ mirror so the AI reads Obsidian notes. Health via container healthcheck. Logs: docker compose logs livesync-bridge"},
    # LTX-trainer (LoRA) is headless (CLI-only, no web UI) — it has no dashboard card. It's a
    # compose service managed via the ops-api (restart), and GPU runs take an exclusive lease
    # via ops-controller. Nothing to surface in the service health grid.
]

# Plugins that are expected to surface a service card. This is the drift tripwire:
# visible_services() warns if the render enables one of these but no SERVICES entry
# claims it — so an enabled service can never be silently omitted from the grid. Core
# services (llamacpp/model-gateway/mcp) carry plugin=None and are intentionally NOT here.
CARD_PLUGINS = frozenset({
    "open-webui", "comfyui", "automation", "rag",
    "hermes-dashboard", "codebase-memory-ui", "voice", "worker",
})


def _load_enabled_plugins() -> set[str] | None:
    """Read `plugins_enabled` from the rendered manifest at MANIFEST_PATH.

    Returns the enabled plugin-id set, or None when the manifest is unavailable
    (env unset / file missing / malformed) so callers FAIL OPEN — a missing mount
    must never blank the grid, it must fall back to showing the full catalog.
    """
    path = os.environ.get("MANIFEST_PATH", "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        logger.warning("[services] could not read manifest %s: %s", path, e)
        return None
    plugins = data.get("plugins_enabled")
    if not isinstance(plugins, list):
        logger.warning("[services] manifest %s has no plugins_enabled list; showing all cards", path)
        return None
    return {str(p) for p in plugins}


def visible_services(services: list[dict] | None = None, enabled: set[str] | None = None) -> list[dict]:
    """The service catalog gated to the render's enabled plugin set (the single source of truth).

    - plugin=None entries (core services) always show.
    - plugin=<id> entries show only when that plugin is enabled in the manifest.
    - FAILS OPEN: when the manifest can't be read (enabled is None), returns the full
      catalog unchanged — the current, pre-manifest behaviour.
    - Drift guard: logs a warning if a CARD_PLUGINS plugin is enabled but no catalog
      entry claims it (an enabled service that would be silently missing from the grid).
    """
    services = SERVICES if services is None else services
    if enabled is None:
        enabled = _load_enabled_plugins()
    if enabled is None:
        return list(services)
    covered = {s.get("plugin") for s in services if s.get("plugin")}
    missing = (CARD_PLUGINS & enabled) - covered
    if missing:
        logger.warning(
            "[services] manifest enables %s but the catalog has no card for them — "
            "service-grid drift; add a SERVICES entry so the enabled service isn't hidden",
            sorted(missing),
        )
    return [s for s in services if s.get("plugin") is None or s["plugin"] in enabled]


async def _check_service(url: str, client: _httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Check if a service is reachable. Returns (ok, error_message)."""
    try:
        c = client or _httpx.AsyncClient(timeout=3.0)
        try:
            r = await c.get(url)
            return (r.status_code < 500, "")
        finally:
            if client is None:
                await c.aclose()
    except (_httpx.RequestError, OSError) as e:
        err = str(e).lower()
        if "connection refused" in err or "connection reset" in err:
            return (False, str(e))
        if "remoteprotocolerror" in err or "protocol" in err or "closed" in err or "disconnected" in err:
            return (True, "")
        return (False, str(e))


# ── Dependency panel (GET /api/dependencies) ────────────────────────────────────────────
#
# Core infrastructure that is a genuine runtime dependency but has NO service-grid card
# (no user-facing UI / Open link). It lives here — in the single catalog — rather than in a
# separate registry file. Always present in every render, so no plugin gate.
INFRA_DEPENDENCIES: list[dict[str, Any]] = [
    {"id": "ops-controller", "name": "Ops Controller", "category": "ops",
     "check": "http://ops-controller:9000/health",
     "hint": "Lifecycle/recovery; not on hot path for chat."},
    {"id": "dashboard", "name": "Dashboard", "category": "control",
     "check": "http://localhost:8080/api/health",
     "hint": "Self-check only works when the probe runs inside the dashboard container (uses localhost)."},
]

DEP_DESCRIPTION = (
    "Live dependency probes derived from the single service catalog (manifest-gated) "
    "plus core infrastructure. Sourced from services_catalog — no separate registry."
)


def dependency_services(
    services: list[dict] | None = None, enabled: set[str] | None = None
) -> list[dict]:
    """The manifest-gated dependency view of the single catalog.

    = every VISIBLE service that exposes a health `check` (headless workers with no
    check are excluded so they don't show a false-red), PLUS the always-on core
    INFRA_DEPENDENCIES that have no grid card. This is the one source behind
    /api/dependencies — there is no separate dependency registry.
    """
    probeable = [s for s in visible_services(services, enabled) if s.get("check")]
    return probeable + [dict(e) for e in INFRA_DEPENDENCIES]


async def _probe_one(
    url: str,
    client: _httpx.AsyncClient,
    timeout_sec: float = 3.0,
    *,
    soft_4xx: bool = False,
) -> tuple[bool, float | None, str | None]:
    """Strict health probe: 2xx == up. `soft_4xx` relaxes that to <500 for endpoints
    (e.g. the MCP gateway) that answer a bare GET with a 4xx while still being up."""
    t0 = time.perf_counter()
    try:
        r = await client.get(url, timeout=timeout_sec)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ok = 200 <= r.status_code < 300
        if not ok and soft_4xx and r.status_code < 500:
            ok = True
        err = None if ok else f"HTTP {r.status_code}"
        return ok, latency_ms, err
    except (_httpx.RequestError, OSError) as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return False, latency_ms, str(e)


async def _probe_dependency(entry: dict[str, Any], client: _httpx.AsyncClient) -> dict[str, Any]:
    """Probe one catalog entry and shape it for the /api/dependencies response."""
    url = entry.get("check") or ""
    ok, lat, err = (
        await _probe_one(url, client, soft_4xx=bool(entry.get("check_4xx_ok")))
        if url
        else (False, None, "no check_url")
    )
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "category": entry.get("category"),
        "hint": entry.get("hint", ""),
        "ok": ok,
        "latency_ms": round(lat, 2) if lat is not None else None,
        "error": err,
    }


async def probe_all(client: _httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Build the GET /api/dependencies payload by probing dependency_services().

    Response shape (backward-compatible with the old dependency_registry): a top-level
    {version, description, entries[]} where each entry carries id/name/category/hint plus
    the live ok/latency_ms/error the dashboard's dependency panel renders.
    """
    entries = dependency_services()
    c = client or _httpx.AsyncClient(timeout=3.0, follow_redirects=True)
    try:
        results = await asyncio.gather(*[_probe_dependency(e, c) for e in entries])
    finally:
        if client is None:
            await c.aclose()
    return {"version": 1, "description": DEP_DESCRIPTION, "entries": list(results)}
