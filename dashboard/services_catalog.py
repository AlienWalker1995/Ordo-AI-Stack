"""Service list for dashboard health/UI and ops ID mapping. Separated from app.py for maintainability."""
from __future__ import annotations

import json
import logging
import os

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
    {"id": "llamacpp", "name": "llama.cpp", "port": 8080, "url": "http://localhost:8080", "check": "http://llamacpp:8080/health", "has_gpu": True, "plugin": None,
     "hint": "Backend-only; use model-gateway :11435 from host. Run: docker compose up -d llamacpp"},
    {"id": "model-gateway", "name": "Model Gateway", "port": 11435, "url": "http://localhost:11435", "check": "http://model-gateway:11435/health/liveliness", "has_gpu": False, "plugin": None,
     "hint": "OpenAI-compatible proxy (LiteLLM). Routes inference to llama.cpp."},
    {"id": "webui", "name": "Open WebUI", "port": 3000, "url": "http://localhost:3000", "check": "http://open-webui:8080", "has_gpu": False, "plugin": "open-webui",
     "hint": "Uses model-gateway for chat. Check: docker compose logs open-webui"},
    {"id": "mcp", "name": "MCP Gateway", "port": 8811, "url": "http://localhost:8811", "check": "http://mcp-gateway:8811/mcp", "has_gpu": False, "plugin": None,
     "hint": "Add/remove tools from the dashboard. Connect at http://localhost:8811/mcp — see docker/mcp-gateway/README.md"},
    {"id": "comfyui", "name": "ComfyUI", "port": 8188, "url": "http://localhost:8188", "check": "http://comfyui:8188", "has_gpu": True, "plugin": "comfyui",
     "hint": "ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU). Run ./compose up -d. Pull LTX-2 via dashboard."},
    {"id": "n8n", "name": "N8N", "port": 5678, "url": "http://localhost:5678", "check": "http://n8n:5678", "has_gpu": False, "plugin": "automation",
     "hint": "Check: docker compose logs n8n"},
    {"id": "qdrant", "name": "Qdrant", "port": 6333, "url": "http://localhost:6333", "has_gpu": False, "plugin": "rag",
     "check": "http://qdrant:6333/readyz",
     "hint": "Vector DB for RAG. Drop files in data/rag-input/ (with --profile rag) or upload via Open WebUI Documents tab."},
    # Hermes Agent runs as two compose services (hermes-gateway + hermes-dashboard). The dashboard
    # container probes via internal DNS — unhealthy means the Hermes services haven't started.
    {"id": "hermes", "name": "Hermes Agent", "port": 9119, "url": "http://localhost:9119",
     "check": "http://hermes-dashboard:9119/", "has_gpu": False, "plugin": "hermes-dashboard",
     "hint": "Managed by docker compose. Logs: docker compose logs hermes-dashboard"},
    # Opt-in (--profile codebase-memory). 3D code knowledge-graph visualization, served at
    # https://<host>/codebase-memory/ on its own SSO-gated port :8448 (the codebase-memory-ui
    # container's nginx serves it under that subpath). The "Open" link comes from SSO_ROUTES
    # in the frontend (-> /codebase-memory/), so no `url` is needed. The health check hits the
    # nginx subpath, which proxies through to the UI.
    {"id": "codebase-memory-ui", "name": "Codebase Memory", "port": 9750,
     "check": "http://codebase-memory-ui:9750/codebase-memory/", "has_gpu": False, "plugin": "codebase-memory-ui",
     "hint": "3D code knowledge-graph. Open at https://<host>:8448/codebase-memory/ (Google SSO). "
             "In-memory index — re-index after a restart. Opt-in: --profile codebase-memory"},
    # ── Voice (--profile voice / plugin `voice`) ──────────────────────────────────────────
    # STT + TTS both pin the 1070 (the 5090 lacks the kernels). Check URLs mirror each
    # service's compose healthcheck (confirmed 200 endpoints), probed via internal DNS.
    {"id": "stt", "name": "Speech-to-Text (Whisper)", "port": 8000, "check": "http://stt:8000/v1/models", "has_gpu": True, "plugin": "voice",
     "hint": "faster-whisper-server (OpenAI-compatible). GPU-pinned to the 1070. Opt-in: --profile voice"},
    {"id": "tts", "name": "Text-to-Speech (Kokoro)", "port": 8880, "check": "http://tts:8880/v1/audio/voices", "has_gpu": True, "plugin": "voice",
     "hint": "kokoro-fastapi (OpenAI-compatible). GPU-pinned to the 1070. Opt-in: --profile voice"},
    # ── Headless background workers ───────────────────────────────────────────────────────
    # worker (plugin `worker`, profile media) and rag-ingestion (plugin `rag`) expose NO HTTP
    # port — their only liveness signal is a file-heartbeat container healthcheck, unreachable
    # from this container. So they carry NO `check` (card shows a neutral "unknown" state, not a
    # false-red or a guessed URL) but ARE operator-controllable via the ops-api (start/stop/restart).
    {"id": "worker", "name": "Media Worker", "port": None, "check": None, "has_gpu": False, "plugin": "worker",
     "hint": "Headless ComfyUI render worker (no web UI). Health via container healthcheck. Logs: docker compose logs worker"},
    {"id": "rag-ingestion", "name": "RAG Ingestion", "port": None, "check": None, "has_gpu": False, "plugin": "rag",
     "hint": "Headless folder-watch embedder for Qdrant (no web UI). Health via container healthcheck. Logs: docker compose logs rag-ingestion"},
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
