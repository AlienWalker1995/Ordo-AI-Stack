"""Install-script generation for OpenClaude on remote tailnet devices.

This module is import-safe (no side effects). All I/O is async or pure;
the FastAPI router in routes_openclaude.py wires it to HTTP.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx


class HostnameResolutionError(RuntimeError):
    """Raised when no usable tailnet hostname can be determined."""


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def resolve_tailnet_hostname(host_header: str | None) -> str:
    """Determine the tailnet-visible hostname the install script should embed.

    Resolution order:
      1. TS_HOSTNAME env var (explicit override)
      2. The Host header sent by the browser when the user opened the dashboard
         (Strip any :port suffix.)

    Raises HostnameResolutionError if neither yields a non-loopback hostname.
    """
    explicit = (os.environ.get("TS_HOSTNAME") or "").strip()
    if explicit:
        return explicit

    if not host_header:
        raise HostnameResolutionError(
            "Cannot determine tailnet hostname. Set TS_HOSTNAME in the dashboard env, "
            "or open the dashboard via your tailnet hostname (e.g. http://my-host.tailXXXX.ts.net:8080)."
        )

    bare = host_header.split(":", 1)[0].strip().lower()
    if bare in _LOOPBACK_HOSTS or bare.endswith(".localhost"):
        raise HostnameResolutionError(
            f"Host header is loopback ({host_header!r}). Set TS_HOSTNAME or open the dashboard "
            "via your tailnet hostname so remote devices can reach this host."
        )
    return bare


class BlogMcpPreflight:
    """Caches the result of a quick reachability probe to the blog MCP server."""

    def __init__(self, url: str, ttl_seconds: float = 10.0, request_timeout: float = 2.0) -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.request_timeout = request_timeout
        self._cache: tuple[float, bool] | None = None
        self._lock = asyncio.Lock()

    async def is_reachable(self, client: httpx.AsyncClient) -> bool:
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and (now - self._cache[0]) < self.ttl_seconds:
                return self._cache[1]
            try:
                response = await client.get(self.url, timeout=self.request_timeout)
                ok = response.status_code < 500
            except httpx.RequestError:
                ok = False
            self._cache = (now, ok)
            return ok


from pathlib import Path

import jinja2

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)


def render_claude_json(
    *,
    host: str,
    mcp_gateway_port: int,
    blog_reachable: bool,
    blog_port: int,
    blog_api_key: str,
    local_workspace_path: str,
) -> str:
    """Render the body of ~/.openclaude/.claude.json for a remote device."""
    template = _jinja_env.get_template("openclaude_claude_json.j2")
    return template.render(
        host=host,
        mcp_gateway_port=mcp_gateway_port,
        blog_port=blog_port,
        blog_api_key=blog_api_key,
        local_workspace_path=local_workspace_path,
        include_blog=bool(blog_reachable and blog_api_key),
    )


def render_install_script_sh(
    *,
    host: str,
    model_gateway_port: int,
    mcp_gateway_port: int,
    master_key: str,
    blog_reachable: bool,
    blog_port: int,
    blog_api_key: str,
    context_window: int = 120000,
) -> str:
    """Render the POSIX install script (macOS / Linux)."""
    template = _jinja_env.get_template("openclaude_install.sh.j2")
    return template.render(
        host=host,
        model_gateway_port=model_gateway_port,
        mcp_gateway_port=mcp_gateway_port,
        master_key=master_key,
        blog_port=blog_port,
        blog_api_key=blog_api_key,
        include_blog=bool(blog_reachable and blog_api_key),
        context_window=context_window,
    )


def render_install_script_ps1(
    *,
    host: str,
    model_gateway_port: int,
    mcp_gateway_port: int,
    master_key: str,
    blog_reachable: bool,
    blog_port: int,
    blog_api_key: str,
    context_window: int = 120000,
) -> str:
    """Render the PowerShell install script (Windows)."""
    template = _jinja_env.get_template("openclaude_install.ps1.j2")
    return template.render(
        host=host,
        model_gateway_port=model_gateway_port,
        mcp_gateway_port=mcp_gateway_port,
        master_key=master_key,
        blog_port=blog_port,
        blog_api_key=blog_api_key,
        include_blog=bool(blog_reachable and blog_api_key),
    )
