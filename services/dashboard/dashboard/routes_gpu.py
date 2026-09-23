"""GPU tab routes: live multi-GPU stats + per-service GPU assignment.

Enumeration is local (gpu_stats). Assignment file ops are delegated to
ops-controller (it owns /workspace), proxied via _ops_request.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from dashboard import gpu_stats

router = APIRouter()

_SMALL_SERVICE_GB = 1.5
_COMFY_DEFAULT_GB = 12.0
_KV_HEADROOM = 1.15


def estimate_service_vram_gb(service: str, model_size_gb: float | None) -> float:
    """Best-effort VRAM need for the capacity guard."""
    if service == "llamacpp":
        base = model_size_gb if model_size_gb else 8.0
        return round(base * _KV_HEADROOM, 1)
    if service == "llamacpp-embed":
        return _SMALL_SERVICE_GB
    if service == "comfyui":
        return _COMFY_DEFAULT_GB
    return _SMALL_SERVICE_GB


def capacity_check(need_gb: float, gpu_total_gb: float) -> dict:
    """Block placements that clearly won't fit (need > total)."""
    if need_gb > gpu_total_gb:
        return {"ok": False, "reason": f"needs ~{need_gb} GB but GPU has {gpu_total_gb} GB total"}
    return {"ok": True, "reason": None}


class GpuAssignRequest(BaseModel):
    service: str
    gpu_uuid: str
    model_size_gb: float | None = None
    confirm: bool = False


def register(app, ops_request):
    """Wire routes onto `app`. `ops_request` is dashboard.app._ops_request."""

    @router.get("/api/gpu/list")
    async def gpu_list(request: Request):
        info = gpu_stats.list_gpus()
        code, data = await ops_request("GET", "/gpu/assignments", request=request)
        assignments = data.get("assignments", {}) if code == 200 else {}
        return {**info, "assignments": assignments}

    app.include_router(router)
