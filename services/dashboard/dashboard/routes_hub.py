"""Hub: the service list (read in-process by the console pages) and aggregated health."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from dashboard.services_catalog import (
    OPS_SERVICE_MAP,
    _check_service,
    mcp_external_url,
    service_open_url,
    visible_services,
)

router = APIRouter(prefix="/api", tags=["hub"])


async def services():
    """Service links and live health status."""
    from dashboard.app import _get_http_client, _ops_request
    client = _get_http_client()

    # Container state/health from ops-controller (docker.sock). The ONLY liveness signal for the
    # headless background workers (worker, rag-ingestion, livesync-bridge) that expose no HTTP
    # `check` — without it their card would show a neutral "unknown". Fetched ONCE here (not per
    # service) and merged into the check:None branch below. Fails soft: if the control plane is down
    # or the token is unset, those services fall back to "unknown", never a false-red.
    container_by_id: dict[str, dict] = {}
    code, data = await _ops_request("GET", "/services")
    if code == 200 and isinstance(data.get("services"), list):
        container_by_id = {c["id"]: c for c in data["services"] if c.get("id")}

    def _container_health(svc_id: str) -> tuple[bool | None, str]:
        """(ok, error) from container state/health for a service with no HTTP check.
        Returns (None, "") — a neutral 'unknown' — when the control plane has no row for it.

        the control plane keys /services by COMPOSE service name, not always the card id
        (`hermes` -> `hermes-dashboard`). Resolve through OPS_SERVICE_MAP first or the
        lookup misses and a running service renders as a permanent grey 'unknown'."""
        c = container_by_id.get(OPS_SERVICE_MAP.get(svc_id, svc_id))
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
        # Server-owned Open link, one source of truth (no hostname guess in the browser), and
        # ONE resolver for every card rather than a per-service branch here: a card gets its
        # clean per-service tailnet name (https://chat.<domain>/ …) when the sidecar layer is
        # enabled, else its own SSO-gated Caddy port root when it declares `sso_port`
        # (model-gateway :8449/ui/, langfuse :8450/), else None so the frontend falls back to
        # its own route rather than rendering a link to a host that does not exist.
        open_url = service_open_url(svc)
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


