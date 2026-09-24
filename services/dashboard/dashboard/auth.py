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
  3. Local mode only (the render turned the edge off, so no SSO identity exists): the local
     operator's session cookie. The browser gets it by presenting DASHBOARD_LOCAL_LOGIN_TOKEN to
     the sign-in route. Only secrets.env and this container hold that token: the render passes it
     to no other service, and to none at all with the edge on. The cookie is an HMAC over its
     expiry, keyed from the token, so nothing on the stack network can mint one and rotating the
     token revokes every session. It is HttpOnly and SameSite=Strict, so another site cannot ride
     it. The source address is never trusted: a request through the published loopback port
     reaches the container from a gateway address that says nothing about who sent it.

Health and read-only views stay open, as before.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import socket
import time

from dashboard import settings

logger = logging.getLogger(__name__)

# The compose service name of the SSO edge (services/edge/plugin.yaml). Resolved per check, not
# pinned to a subnet: the stack network is flat, and its subnet changes when it is recreated.
EDGE_PROXY_HOST = "caddy"
EDGE_IDENTITY_HEADER = "X-Forwarded-Email"

# Local mode: where the browser exchanges the sign-in token for a session cookie, and that cookie.
LOCAL_SIGN_IN_PATH = "/api/auth/local/sign-in"
LOCAL_SESSION_COOKIE = "ordo_local_session"
LOCAL_SESSION_SECONDS = 30 * 24 * 3600
LOCAL_OPERATOR = "local-operator"
# Domain separation: the cookie is signed with a key derived from the token, never the token itself.
_LOCAL_SESSION_KEY_LABEL = b"ordo-dashboard-local-session-v1"

# The sign-in route needs no principal: presenting the token IS the authentication.
PUBLIC_API_PATHS = frozenset({"/api/health", "/api/orchestration/readiness", LOCAL_SIGN_IN_PATH})
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


def local_mode() -> bool:
    """True when the render passed the local sign-in token, which it does only with the edge off."""
    return bool(settings.DASHBOARD_LOCAL_LOGIN_TOKEN)


def local_token_matches(presented: str, token: str) -> bool:
    """True when `presented` is the configured, non-empty local sign-in token."""
    if not token:
        return False
    return hmac.compare_digest(presented.strip().encode(), token.encode())


def _local_session_signature(token: str, expires: int) -> str:
    key = hmac.new(token.encode(), _LOCAL_SESSION_KEY_LABEL, hashlib.sha256).digest()
    return hmac.new(key, f"{LOCAL_OPERATOR}|{expires}".encode(), hashlib.sha256).hexdigest()


def local_session_cookie(token: str, *, now: float) -> str:
    """The local operator's session cookie value, `<expiry epoch>.<hmac>`, valid for
    LOCAL_SESSION_SECONDS from `now`."""
    expires = int(now) + LOCAL_SESSION_SECONDS
    return f"{expires}.{_local_session_signature(token, expires)}"


def local_session_valid(cookie: str, token: str, *, now: float) -> bool:
    """True for an unexpired cookie signed with the current token. Always False in edge mode."""
    if not token or not cookie:
        return False
    expires_text, _, signature = cookie.partition(".")
    if not (expires_text.isascii() and expires_text.isdigit()):
        return False
    expires = int(expires_text)
    if expires <= now:
        return False
    return hmac.compare_digest(signature.encode(), _local_session_signature(token, expires).encode())


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


def principal(headers, peer_ip: str | None, edge_addresses: set[str],
              local_session: str = "") -> str | None:
    """The authenticated caller, or None.

    `headers` is the request's header mapping, `peer_ip` its TCP peer (uvicorn runs with
    --no-proxy-headers, so this is never rewritten from X-Forwarded-For), and `local_session` the
    LOCAL_SESSION_COOKIE value, if the request carried one."""
    if bearer_matches(headers.get("Authorization", ""), settings.OPS_CONTROLLER_TOKEN):
        return "ops-controller-bearer"
    if local_session_valid(local_session, settings.DASHBOARD_LOCAL_LOGIN_TOKEN, now=time.time()):
        return LOCAL_OPERATOR
    email = headers.get(EDGE_IDENTITY_HEADER, "").strip()
    if email and _is_edge_peer(peer_ip, edge_addresses):
        return email
    return None


async def request_principal(request) -> str | None:
    """principal() for a Starlette request. The edge name is resolved only when an identity is
    claimed, so an ordinary request does no DNS lookup."""
    edge_addresses: set[str] = set()
    if request.headers.get(EDGE_IDENTITY_HEADER, "").strip():
        edge_addresses = await asyncio.to_thread(edge_proxy_addresses)
    peer_ip = request.client.host if request.client else None
    return principal(request.headers, peer_ip, edge_addresses,
                     local_session=request.cookies.get(LOCAL_SESSION_COOKIE, ""))
