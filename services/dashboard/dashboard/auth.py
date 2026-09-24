"""Who may make the dashboard act on the stack.

The dashboard forwards operator actions to ops-controller with its own OPS_CONTROLLER_TOKEN. It
must therefore act only for a caller who could have acted directly, or it becomes a confused
deputy that lets any container on the stack network step around ops-controller's bearer auth.

A request that changes state, or that reaches an ops-forwarding namespace, needs a principal:

  1. The SSO edge identity: `X-Forwarded-Email`, trusted only when the TCP peer is the Caddy
     edge itself. Caddy sets that header from oauth2-proxy's forward_auth response and strips any
     client-supplied copy (tests/test_caddyfile_invariants.py), so a browser cannot forge it, and
     a container that connects to dashboard:8080 directly is not the edge peer. The containers
     that share Caddy's network namespace (hermes-dashboard, the tailnet sidecars) do present its
     address; hermes-dashboard already holds OPS_CONTROLLER_TOKEN, so that gains it nothing.
  2. `Authorization: Bearer <OPS_CONTROLLER_TOKEN>`: an internal caller (mcp-orchestration) that
     already holds the ops-controller credential gains nothing it could not do directly.

Health and read-only views stay open, as before.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import socket

from dashboard import settings

logger = logging.getLogger(__name__)

# The compose service name of the SSO edge (services/edge/plugin.yaml). Resolved per check, not
# pinned to a subnet: the stack network is flat, and its subnet changes when it is recreated.
EDGE_PROXY_HOST = "caddy"
EDGE_IDENTITY_HEADER = "X-Forwarded-Email"

PUBLIC_API_PATHS = frozenset({"/api/health", "/api/orchestration/readiness"})
# Namespaces whose every route forwards to ops-controller or drives stack operations, reads included.
OPS_FORWARDING_PREFIXES = ("/api/ops/", "/api/orchestration/")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def requires_principal(method: str, path: str) -> bool:
    """True when this /api/* request must come from an authenticated principal."""
    if path in PUBLIC_API_PATHS:
        return False
    if method.upper() not in SAFE_METHODS:
        return True
    return path.startswith(OPS_FORWARDING_PREFIXES)


def bearer_matches(authorization: str, token: str) -> bool:
    """True when `authorization` is `Bearer <token>` for a configured, non-empty token."""
    if not token:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme != "Bearer":
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def edge_proxy_addresses() -> set[str]:
    """The addresses the edge proxy resolves to on the dashboard's networks (blocking DNS).

    An empty set (the name does not resolve) means no request can carry the edge identity."""
    try:
        infos = socket.getaddrinfo(EDGE_PROXY_HOST, None)
    except OSError as exc:
        logger.debug("cannot resolve the edge proxy %s: %s", EDGE_PROXY_HOST, exc)
        return set()
    return {str(ipaddress.ip_address(info[4][0])) for info in infos}


def _is_edge_peer(peer_ip: str | None, edge_addresses: set[str]) -> bool:
    if not peer_ip:
        return False
    try:
        return str(ipaddress.ip_address(peer_ip)) in edge_addresses
    except ValueError:
        return False


def principal(headers, peer_ip: str | None, edge_addresses: set[str]) -> str | None:
    """The authenticated caller, or None.

    `headers` is the request's header mapping, `peer_ip` its TCP peer (uvicorn runs with
    --no-proxy-headers, so this is never rewritten from X-Forwarded-For)."""
    if bearer_matches(headers.get("Authorization", ""), settings.OPS_CONTROLLER_TOKEN):
        return "ops-controller-bearer"
    email = headers.get(EDGE_IDENTITY_HEADER, "").strip()
    if email and _is_edge_peer(peer_ip, edge_addresses):
        return email
    return None
