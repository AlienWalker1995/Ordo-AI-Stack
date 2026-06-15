"""Registry tab routes: model registry + GPU view passthrough to ops-controller."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/registry")

# Injected by register()
_ops_request = None


def register(app, ops_request):
    """Wire routes onto `app`. `ops_request` is dashboard.app._ops_request."""
    global _ops_request
    _ops_request = ops_request

    @router.get("/models")
    async def list_models(request: Request):
        code, data = await _ops_request("GET", "/registry/models", request=request)
        return data

    @router.get("/gpus")
    async def list_gpus(request: Request):
        code, data = await _ops_request("GET", "/registry/gpus", request=request)
        return data

    @router.get("/models/{model_id}")
    async def get_model(model_id: str, request: Request):
        code, data = await _ops_request("GET", f"/registry/models/{model_id}", request=request)
        return data

    @router.post("/models")
    async def define_model(body: dict, request: Request):
        code, data = await _ops_request(
            "POST", "/registry/models", request=request,
            json={**body, "actor": "dashboard"},
        )
        return data

    @router.delete("/models/{model_id}")
    async def delete_model(model_id: str, request: Request):
        code, data = await _ops_request(
            "DELETE", f"/registry/models/{model_id}", request=request,
        )
        return data

    @router.post("/models/{model_id}/assign-gpu")
    async def assign_gpu(model_id: str, body: dict, request: Request):
        code, data = await _ops_request(
            "POST", f"/registry/models/{model_id}/assign-gpu", request=request,
            json=body,
        )
        return data

    @router.post("/models/{model_id}/enable")
    async def enable_model(model_id: str, body: dict, request: Request):
        code, data = await _ops_request(
            "POST", f"/registry/models/{model_id}/enable", request=request,
            json=body,
        )
        return data

    app.include_router(router)
