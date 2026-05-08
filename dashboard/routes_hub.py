"""Public hub routes: service list, auth config, aggregated health."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from dashboard.dependency_registry import probe_all
from dashboard.services_catalog import SERVICES, _check_service
from dashboard.settings import AUTH_REQUIRED

router = APIRouter(prefix="/api", tags=["hub"])


@router.get("/services")
async def services():
    """Service links and live health status."""
    from dashboard.app import _get_http_client
    client = _get_http_client()

    async def _probe(svc: dict) -> dict:
        ok, err = await _check_service(svc["check"], client) if svc.get("check") else (None, "")
        return {
            **{k: v for k, v in svc.items() if k != "check"},
            "ok": ok,
            "error": err if not ok else None,
            "hint": svc.get("hint", ""),
        }

    results = await asyncio.gather(*[_probe(s) for s in SERVICES])
    return {"services": list(results)}


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

    results = await asyncio.gather(*[_probe(s) for s in SERVICES])
    all_ok = all(r["ok"] for r in results if r["ok"] is not None)
    return {"ok": all_ok, "services": list(results)}


@router.get("/dependencies")
async def dependencies():
    """Canonical dependency registry with live probes (M7). No auth required."""
    from dashboard.app import _get_http_client
    return await probe_all(_get_http_client())
