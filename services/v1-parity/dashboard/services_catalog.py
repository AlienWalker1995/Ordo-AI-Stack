"""Single source of truth for "what services exist + how to probe them".

The catalog is DATA, not code: every service declares its dashboard card(s) in a
`services/<id>/catalog.json` fragment co-located with its other manifests (plugin.yaml /
agent.yaml / dashboard.yaml). `ordo render` aggregates the fragments into
out/services-catalog.json, which the dashboard container mounts read-only
(SERVICES_CATALOG_PATH — same pattern as the manifest mount). In-repo (tests / dev) the
fragments are read directly, so both paths serve the identical card list.

Feeds three surfaces, all derived from the one loaded `SERVICES` catalog:
  * the service grid   — GET /api/services, /api/health (visible_services())
  * ops lifecycle wiring — OPS_SERVICE_MAP (derived from each card's `ops_service`)
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
from pathlib import Path
from typing import Any

import httpx as _httpx

logger = logging.getLogger(__name__)


def mcp_external_url() -> str | None:
    """The MCP gateway's real external endpoint: the Bearer-gated /mcp route on the
    :443 front door (https://<host>/mcp) — NOT a :8811 port, which isn't published.
    Returns None when the edge host is unknown so the frontend keeps its fallback."""
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    return f"https://{host}/mcp" if host else None


# LiteLLM auto-generates its OpenAPI docs and serves the Swagger UI at its ROOT path
# (`GET model-gateway:11435/` returns the swagger HTML; `/docs` 404s — confirmed by probe).
#
# It MUST be opened at an origin ROOT, which is why this is a subdomain (llm.<tailnet> →
# caddy :8449) and not `/llm/`. The :443 `/llm/*` route strips its prefix, and the swagger
# HTML then references ROOT-ABSOLUTE assets (`/swagger/swagger-ui.css`, `/swagger/…js`).
# Through `/llm/` the document returns 200 but every asset resolves to
# `https://<host>/swagger/…`, escapes the `/llm/*` handler, hits the front door and 404s —
# a blank page that reads as an outage. `/llm/*` remains the SSO-BYPASSING API base for
# programmatic clients (LITELLM_MASTER_KEY bearer); it is not a browser entry.
MODEL_GATEWAY_SWAGGER_PORT = 8449


def model_gateway_open_url() -> str | None:
    """Browsable Open link for the model-gateway card.

    Prefers the subdomain (`https://llm.<domain>/`) like every other service; falls back to
    the SSO'd port root (`https://<host>:8449/`) when the sidecar layer is disabled, and to
    None when the edge host is unknown so the frontend emits no broken link."""
    subdomain = tailnet_open_url("model-gateway")
    if subdomain:
        return subdomain
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    return f"https://{host}:{MODEL_GATEWAY_SWAGGER_PORT}/" if host else None


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

# ── Catalog loading (the card list is JSON, declared per-service) ──────────────────────
# Card schema = the grid fields (id/name/port/url/check/check_4xx_ok/has_gpu/plugin/
# category/background/hint) plus the wiring keys `ops_service` (compose service targeted
# by the card's lifecycle buttons -> OPS_SERVICE_MAP) and `tailnet_label` (clean
# subdomain -> TAILNET_LABELS), plus `order` (curated grid order — aggregation sorts by
# it so glob order never reshuffles the UI). `notes` is rationale for humans reading the
# fragment; the API/frontend ignore it.
SERVICES_CATALOG_ENV = "SERVICES_CATALOG_PATH"
# In-repo location of the fragments: this file lives at services/v1-parity/dashboard/,
# so parents[2] is the shared services/ root the render registries also glob.
_REPO_SERVICES_DIR = Path(__file__).resolve().parents[2]


def _sorted_cards(cards: list[dict]) -> list[dict]:
    """Deterministic curated order: explicit `order` first, id as the tiebreak."""
    return sorted(cards, key=lambda c: (int(c.get("order", 1000)), str(c.get("id", ""))))


def _load_catalog_cards() -> list[dict]:
    """Load the card list: rendered aggregate first (runtime), repo fragments second (dev/tests).

    There is deliberately NO hardcoded fallback list — the JSON fragments are the single
    source of truth, and a baked-in shadow copy would be exactly the drift this refactor
    removes. Both sources missing is a deploy error (check the services-catalog.json mount
    `ordo render` emits): it logs loudly and the grid renders empty rather than stale.
    """
    path = os.environ.get(SERVICES_CATALOG_ENV, "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            data = None
            logger.error("[services] could not read catalog %s: %s; trying repo fragments", path, e)
        if isinstance(data, dict) and isinstance(data.get("services"), list):
            return _sorted_cards([dict(c) for c in data["services"]])
        if data is not None:
            logger.error("[services] %s has no `services` list; trying repo fragments", path)
    cards: list[dict] = []
    for frag in sorted(_REPO_SERVICES_DIR.glob("*/catalog.json")):
        try:
            frag_data = json.loads(frag.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error("[services] skipping malformed catalog fragment %s: %s", frag, e)
            continue
        cards.extend(dict(c) for c in (frag_data.get("cards") or []))
    if not cards:
        logger.error(
            "[services] NO service catalog found (%s unset/unreadable and no services/*/catalog.json "
            "fragments) — the grid will be empty. Deploy error: check the services-catalog.json "
            "mount emitted by `ordo render`.", SERVICES_CATALOG_ENV)
    return _sorted_cards(cards)


SERVICES = _load_catalog_cards()

# Dashboard service id -> ops-controller (compose) service id, derived from each card's
# `ops_service`. Every value MUST be a real compose service name AND be present in
# ops-api's ALLOWED_SERVICES, else the card's start/stop/restart buttons 400. (Locked by
# test_service_catalog_wiring.) Cards without `ops_service` (e.g. couchdb) fall back to
# their own id at the call sites (OPS_SERVICE_MAP.get(id, id)). NB: Hermes deliberately
# maps to hermes-dashboard (the UI service), NOT the agent/gateway — its self-restart is
# delicate and it is not allowlisted.
OPS_SERVICE_MAP = {s["id"]: s["ops_service"] for s in SERVICES if s.get("ops_service")}

# Dashboard service id -> clean per-service tailnet subdomain label (the tailnet-names
# sidecar plugin serves each UI as https://<label>.<domain>/), derived from each card's
# `tailnet_label`. Only UI services have a sidecar; backend-only services (llamacpp/mcp/
# qdrant) have no clean name and keep their internal URLs. hermes/graph land on their
# port's root, which 302s to the /hermes/ and /codebase-memory/ subpaths, so a bare
# https://<label>.<domain>/ works. model-gateway's `llm` sidecar (caddy :8449) serves the
# LiteLLM Swagger UI at an origin root.
TAILNET_LABELS = {s["id"]: s["tailnet_label"] for s in SERVICES if s.get("tailnet_label")}

# ── Card semantics (apply to every fragment) ───────────────────────────────────────────
# `plugin` names the render plugin (manifest.plugins_enabled id) that gates the card.
# Core services always present in every render carry plugin=null and are never gated.
# visible_services() (below) hides a card only when its plugin is DISABLED, so the grid
# reflects what the render actually enabled. NB: the plugin id is NOT the compose profile
# (e.g. open-webui's profile is `webui` but its plugin id is `open-webui`).
#
# `background: true` is the marker the frontend uses to move a service OUT of the main
# user-facing grid into the secondary "Background jobs" section (no "Open" link). Its
# meaning is "NOT a browsable user-facing UI" — not merely "headless worker". Besides the
# portless workers (rag-ingestion, livesync-bridge — no port, no check; the grid reads
# their true up/down from ops-api container health, see routes_hub) it ALSO tags the
# infra/backend services that have a port & health check but no browsable UI a person
# visits: llamacpp, llamacpp-cpu, mcp, qdrant, stt, tts, couchdb. The main grid is ONLY
# the user-facing UIs (webui/comfyui/n8n/hermes/codebase-memory-ui) plus model-gateway
# (its Open link points at the LiteLLM Swagger UI through the edge; see
# model_gateway_open_url() below).
#
# Deliberately card-LESS services: ltx-trainer (CLI-only LoRA trainer — ops-api-managed,
# GPU runs take an ops-controller lease); the obsidian-livesync Funnel (a Tailscale
# config sidecar, not a browsable service); and the retired Media Worker (the live media
# pipeline runs via Hermes cron + direct render_publish scripts — see CHANGELOG).

# Plugins that are expected to surface a service card. This is the drift tripwire:
# visible_services() warns if the render enables one of these but no catalog fragment
# claims it — so an enabled service can never be silently omitted from the grid. Core
# services (llamacpp/model-gateway/mcp) carry plugin=null and are intentionally NOT here.
# Kept EXPLICIT (not derived from the fragments) on purpose: deriving it from the same
# fragments it guards would blind the tripwire to a deleted fragment.
CARD_PLUGINS = frozenset({
    "open-webui", "comfyui", "automation", "rag",
    "hermes-dashboard", "codebase-memory-ui", "voice",
    "llamacpp-cpu",
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
            "service-grid drift; add a services/<id>/catalog.json fragment so the enabled "
            "service isn't hidden",
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
