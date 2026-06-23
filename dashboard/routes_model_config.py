"""Model-config control-plane passthrough to ops-controller (/model-config).

Thin proxy (like routes_registry): the browser never sees the ops-controller
token — _ops_request injects it. Drives the dashboard's Model Control flag UI.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/model-config")

_ops_request = None
_registered = False


def register(app, ops_request):
    """Wire routes onto `app`. `ops_request` is dashboard.app._ops_request."""
    global _ops_request, _registered
    _ops_request = ops_request
    if _registered:
        return
    _registered = True

    @router.get("")
    async def get_model_config(request: Request):
        code, data = await _ops_request("GET", "/model-config", request=request)
        if code >= 400:
            raise HTTPException(status_code=code, detail=(data.get("detail", data) if isinstance(data, dict) else data))
        return data

    @router.post("")
    async def post_model_config(body: dict, request: Request):
        code, data = await _ops_request("POST", "/model-config", request=request, json=body)
        if code >= 400:
            raise HTTPException(status_code=code, detail=(data.get("detail", data) if isinstance(data, dict) else data))
        return data

    app.include_router(router)
