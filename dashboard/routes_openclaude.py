"""FastAPI routes for OpenClaude self-serve install on remote tailnet devices."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from dashboard.openclaude_install import (
    BlogMcpPreflight,
    HostnameResolutionError,
    render_install_script_ps1,
    render_install_script_sh,
    resolve_tailnet_hostname,
)

router = APIRouter(tags=["openclaude"])

_DEFAULT_DASHBOARD_PORT = 8080
_DEFAULT_MODEL_GATEWAY_PORT = 11435
_DEFAULT_MCP_GATEWAY_PORT = 8811
_DEFAULT_BLOG_PORT = 3500

_blog_preflight = BlogMcpPreflight(
    url=f"http://host.docker.internal:{_DEFAULT_BLOG_PORT}/mcp",
    ttl_seconds=10.0,
)


def _ports() -> tuple[int, int, int, int]:
    return (
        int(os.environ.get("DASHBOARD_PORT", _DEFAULT_DASHBOARD_PORT)),
        int(os.environ.get("MODEL_GATEWAY_PORT", _DEFAULT_MODEL_GATEWAY_PORT)),
        int(os.environ.get("MCP_GATEWAY_PORT", _DEFAULT_MCP_GATEWAY_PORT)),
        int(os.environ.get("BLOG_MCP_PORT", _DEFAULT_BLOG_PORT)),
    )


def _resolve_host_or_503(request: Request) -> str:
    try:
        return resolve_tailnet_hostname(host_header=request.headers.get("host"))
    except HostnameResolutionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/openclaude/preview")
async def preview(request: Request):
    from dashboard.app import _get_http_client
    host = _resolve_host_or_503(request)
    dashboard_port, model_port, mcp_port, blog_port = _ports()
    blog_api_key = os.environ.get("BLOG_MCP_API_KEY", "")
    blog_reachable = await _blog_preflight.is_reachable(_get_http_client())
    return {
        "host": host,
        "model_gateway_url": f"http://{host}:{model_port}/v1",
        "mcp_gateway_url": f"http://{host}:{mcp_port}/mcp",
        "blog_mcp_reachable": bool(blog_reachable and blog_api_key),
        "model": "local-chat",
        "one_liner_ps1": (
            f"irm http://{host}:{dashboard_port}/install/openclaude.ps1 | iex"
        ),
        "one_liner_sh": (
            f"curl -fsSL http://{host}:{dashboard_port}/install/openclaude.sh | bash"
        ),
    }


async def _build_install_render_kwargs(request: Request) -> dict:
    from dashboard.app import _get_http_client
    host = _resolve_host_or_503(request)
    _, model_port, mcp_port, blog_port = _ports()
    blog_api_key = os.environ.get("BLOG_MCP_API_KEY", "")
    blog_reachable = await _blog_preflight.is_reachable(_get_http_client())
    master_key = os.environ.get("LITELLM_MASTER_KEY", "local")
    return dict(
        host=host,
        model_gateway_port=model_port,
        mcp_gateway_port=mcp_port,
        master_key=master_key,
        blog_reachable=blog_reachable,
        blog_port=blog_port,
        blog_api_key=blog_api_key,
    )


@router.get("/install/openclaude.sh", response_class=PlainTextResponse)
async def install_sh(request: Request):
    kwargs = await _build_install_render_kwargs(request)
    body = render_install_script_sh(**kwargs)
    return PlainTextResponse(body, headers={"Cache-Control": "no-store"})


@router.get("/install/openclaude.ps1", response_class=PlainTextResponse)
async def install_ps1(request: Request):
    kwargs = await _build_install_render_kwargs(request)
    body = render_install_script_ps1(**kwargs)
    return PlainTextResponse(body, headers={"Cache-Control": "no-store"})
