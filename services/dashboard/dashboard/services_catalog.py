"""Single source of truth for "what services exist + how to probe them".

The catalog is DATA, not code: every service declares its dashboard card(s) in a
`services/<id>/catalog.json` fragment co-located with its other manifests (plugin.yaml /
agent.yaml / dashboard.yaml). `ordo render` aggregates the fragments into
out/services-catalog.json, which the dashboard container mounts read-only
(SERVICES_CATALOG_PATH — same pattern as the manifest mount). In-repo (tests / dev) the
fragments are read directly, so both paths serve the identical card list.

Feeds two surfaces, both derived from the one loaded `SERVICES` catalog:
  * the service grid   — GET /api/services, /api/health (visible_services())
  * ops lifecycle wiring — OPS_SERVICE_MAP (derived from each card's `ops_service`)

A service's check URL / name / hint / category lives in exactly one place.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx as _httpx

logger = logging.getLogger(__name__)


def mcp_external_url() -> str | None:
    """The MCP gateway's external endpoint: LiteLLM's Bearer-authenticated /mcp on the
    :443 front door (https://<host>/mcp). Returns None when the edge host is unknown so
    the frontend keeps its fallback."""
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    return f"https://{host}/mcp" if host else None


def service_open_url(card: dict) -> str | None:
    """The browsable Open link for one card, resolved SERVER-side so the browser never guesses.

    Three shapes, in order:
      1. the clean per-service tailnet name (`https://<label>.<domain>/`) when the sidecar
         layer is enabled - the same answer every service gets;
      2. the service's own SSO-gated Caddy PORT ROOT (`https://<host>:<sso_port>/`) when the
         card declares `sso_port` and the sidecars are off;
      3. None when no edge hostname is configured, so the frontend falls back to its own route
         rather than rendering a link to a host that does not exist.

    A card may add `sso_path` (default `/`) when the human-facing surface is not the origin
    root - e.g. model-gateway, whose LiteLLM admin UI lives at `/ui/` (swagger stays at `/`).

    Both fallbacks are a port ROOT on purpose. A prefix-stripping subpath route breaks any app
    that emits root-absolute assets: through the edge's `/llm/*` route LiteLLM's admin UI HTML
    returns 200 but every `/ui/_next/*` asset escapes the handler and 404s, rendering a blank
    page that reads as an outage. `/llm/*` stays the SSO-BYPASSING API base for programmatic
    bearer clients; it is not a browser entry. The same reasoning applies to Langfuse (:8450),
    which is why this is one generic resolver and not a per-service special case.
    """
    path = str(card.get("sso_path") or "/")
    subdomain = tailnet_open_url(card.get("id", ""))
    if subdomain:
        return subdomain.rstrip("/") + path
    port = card.get("sso_port")
    host = os.environ.get("CADDY_TAILNET_HOSTNAME", "").strip()
    if port and host:
        return f"https://{host}:{int(port)}{path}"
    return None


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
# by the card's lifecycle buttons -> OPS_SERVICE_MAP), `tailnet_label` (clean
# subdomain -> TAILNET_LABELS) and `sso_port` / `sso_path` (the service's own SSO-gated
# Caddy port root, used by service_open_url() as the Open link when the sidecar layer is
# off), plus `order` (curated grid order - aggregation sorts by it so glob order never
# reshuffles the UI). `notes` is rationale for humans reading the fragment; the
# API/frontend ignore it.
#
# `port`/`url` are the FRONTEND's last-resort direct-link fallback and only make sense for a
# service that actually publishes a host port. A service reached solely through the edge
# (langfuse) declares `sso_port` and omits them: a host:port link would point at nothing, and
# a container port copied in "for completeness" can collide with another card's (3000 is
# open-webui's) and render a confidently wrong link.
SERVICES_CATALOG_ENV = "SERVICES_CATALOG_PATH"
# In-repo location of the fragments: this file lives at services/dashboard/dashboard/,
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
# the control plane's allowlist, else the card's start/stop/restart buttons 400. (Locked by
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
# LiteLLM admin UI at /ui/ (swagger at /) at an origin root.
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
# their true up/down from ops-controller container health, see routes_hub) it ALSO tags the
# infra/backend services that have a port & health check but no browsable UI a person
# visits: llamacpp, llamacpp-cpu, mcp, qdrant, stt, tts, couchdb. The main grid is ONLY
# the user-facing UIs (webui/comfyui/n8n/hermes/codebase-memory-ui/langfuse) plus
# model-gateway (its Open link points at the LiteLLM admin UI at /ui/ through the edge; see
# service_open_url() above).
#
# Deliberately card-LESS services: ltx-trainer (CLI-only LoRA trainer — control-plane-managed,
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
    "llamacpp-cpu", "langfuse",
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
