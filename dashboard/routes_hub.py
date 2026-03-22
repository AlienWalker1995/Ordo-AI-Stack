"""Public hub routes: service list, auth config, aggregated health."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from dashboard.dependency_registry import probe_all
from dashboard.services_catalog import SERVICES, _check_service
from dashboard.settings import AUTH_REQUIRED

router = APIRouter(prefix="/api", tags=["hub"])


@router.get("/services")
async def services():
    """Service links and live health status."""
    results = []
    for svc in SERVICES:
        ok, err = await _check_service(svc["check"]) if svc.get("check") else (None, "")
        results.append({
            **{k: v for k, v in svc.items() if k != "check"},
            "ok": ok,
            "error": err if not ok else None,
            "hint": svc.get("hint", ""),
        })
    return {"services": results}


@router.get("/auth/config")
async def auth_config():
    """Return auth config for frontend. No auth required."""
    if not AUTH_REQUIRED:
        return {"auth_required": False, "auth_type": None}
    return {"auth_required": True, "auth_type": "bearer"}


@router.get("/health")
async def health():
    """Aggregated platform health. Returns ok=true when all services are reachable."""
    results = []
    for svc in SERVICES:
        ok, err = await _check_service(svc["check"]) if svc.get("check") else (None, "")
        results.append({"id": svc["id"], "ok": ok, "error": err})
    all_ok = all(r["ok"] for r in results if r["ok"] is not None)
    return {"ok": all_ok, "services": results}


@router.get("/dependencies")
async def dependencies():
    """Canonical dependency registry with live probes (M7). No auth required."""
    return await asyncio.to_thread(probe_all)
