"""Public hub routes: service list, auth config, aggregated health."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from dashboard.services_catalog import (
    _check_service,
    mcp_external_url,
    model_gateway_open_url,
    probe_all,
    tailnet_open_url,
    visible_services,
)
from dashboard.settings import AUTH_REQUIRED

router = APIRouter(prefix="/api", tags=["hub"])


@router.get("/services")
async def services():
    """Service links and live health status."""
    from dashboard.app import _get_http_client, _ops_request
    client = _get_http_client()

    # Container state/health from ops-api (docker.sock). This is the ONLY liveness signal for the
    # headless background workers (worker, rag-ingestion, livesync-bridge) that expose no HTTP
    # `check` — without it their card would show a neutral "unknown". Fetched ONCE here (not per
    # service) and merged into the check:None branch below. Fails soft: if ops-api is unreachable
    # or the token is unset, those services fall back to "unknown", never a false-red.
    container_by_id: dict[str, dict] = {}
    code, data = await _ops_request("GET", "/services")
    if code == 200 and isinstance(data.get("services"), list):
        container_by_id = {c["id"]: c for c in data["services"] if c.get("id")}

    def _container_health(svc_id: str) -> tuple[bool | None, str]:
        """(ok, error) from container state/health for a service with no HTTP check.
        Returns (None, "") — a neutral 'unknown' — when ops-api has no row for it."""
        c = container_by_id.get(svc_id)
        if not c:
            return None, ""
        state, health = c.get("state"), c.get("health")
        if state != "running":
            return False, f"container {state or 'missing'}"
        if health == "unhealthy":
            return False, "container unhealthy"
        if health == "starting":
            return None, ""  # still coming up — neutral, not red
        return True, ""  # running + (healthy | no healthcheck declared)

    async def _probe(svc: dict) -> dict:
        if svc.get("check"):
            ok, err = await _check_service(svc["check"], client)
        else:
            ok, err = _container_health(svc["id"])
        # Server-owned Open link, one source of truth (no hostname guess in the browser):
        #  * model-gateway is user-facing but has no tailnet sidecar — its Open link points
        #    at the LiteLLM Swagger UI through the edge (https://<host>/llm/).
        #  * the sidecar UIs get their clean per-service tailnet name (https://chat.<domain>/ …)
        #    when the tailnet-names layer is enabled.
        # Either is None when the edge host is unknown, so the frontend falls back to its
        # port/SSO route rather than rendering a broken link.
        open_url = (
            model_gateway_open_url()
            if svc["id"] == "model-gateway"
            else tailnet_open_url(svc["id"])
        )
        return {
            **{k: v for k, v in svc.items() if k != "check"},
            "ok": ok,
            "error": err if not ok else None,
            "hint": svc.get("hint", ""),
            "open_url": open_url,
        }

    # Gate the grid on the render manifest's enabled plugin set so it reflects what's
    # actually deployed (and can't silently omit an enabled service). Fails open to the
    # full catalog when the manifest isn't mounted.
    results = await asyncio.gather(*[_probe(s) for s in visible_services()])
    return {"services": list(results), "mcp_external_url": mcp_external_url()}


@router.get("/auth/config")
async def auth_config(request: Request):
    """Return auth config for frontend. No auth required."""
    if not AUTH_REQUIRED:
        return {"auth_required": False, "auth_type": None}
    # SSO front door: when the request arrives through Caddy's forward_auth
    # with a verified X-Forwarded-Email, the auth middleware will accept it
    # in lieu of a bearer token. Tell the JS no further auth is needed so
    # the bearer modal doesn't pop up on every page load.
    from dashboard.app import _request_from_trusted_proxy
    if _request_from_trusted_proxy(request) and request.headers.get("X-Forwarded-Email", "").strip():
        return {"auth_required": False, "auth_type": None}
    return {"auth_required": True, "auth_type": "bearer"}


@router.get("/health")
async def health():
    """Aggregated platform health. Returns ok=true when all services are reachable."""
    from dashboard.app import _get_http_client
    client = _get_http_client()

    async def _probe(svc: dict) -> dict:
        ok, err = await _check_service(svc["check"], client) if svc.get("check") else (None, "")
        return {"id": svc["id"], "ok": ok, "error": err}

    results = await asyncio.gather(*[_probe(s) for s in visible_services()])
    all_ok = all(r["ok"] for r in results if r["ok"] is not None)
    return {"ok": all_ok, "services": list(results)}


@router.get("/dependencies")
async def dependencies():
    """Live dependency probes derived from the single service catalog. No auth required."""
    from dashboard.app import _get_http_client
    return await probe_all(_get_http_client())
