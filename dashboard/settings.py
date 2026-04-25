"""Environment-derived settings for the dashboard (single source of truth)."""
from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DASHBOARD_AUTH_TOKEN: str = os.environ.get("DASHBOARD_AUTH_TOKEN", "").strip()
AUTH_REQUIRED: bool = bool(DASHBOARD_AUTH_TOKEN)

# Trusted reverse-proxy mode: when enabled, X-Forwarded-Email from a request
# whose source IP is in DASHBOARD_TRUSTED_PROXY_NET is treated as the
# authenticated identity. Bearer-token mode (orchestration-mcp / internal
# calls) still applies for requests outside the trusted network.
DASHBOARD_TRUST_PROXY_HEADERS: bool = (
    os.environ.get("DASHBOARD_TRUST_PROXY_HEADERS", "false").lower() == "true"
)

_proxy_net = os.environ.get("DASHBOARD_TRUSTED_PROXY_NET", "").strip()
DASHBOARD_TRUSTED_PROXY_NET: ipaddress.IPv4Network | ipaddress.IPv6Network | None = (
    ipaddress.ip_network(_proxy_net, strict=False) if _proxy_net else None
)

# Blocked port range: browsers refuse connections to 6666-6669 (IRC).
# Kept as a stack-wide safety utility even without current callers — any future
# service that accepts a port via env var should route through `_validated_port`.
_BLOCKED_PORTS = set(range(6666, 6670))


def _validated_port(name: str, default: str) -> str:
    """Return the env-var value if it's a valid port number, else the default."""
    raw = os.environ.get(name, default).strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 65535):
        logger.warning("Invalid %s=%r — falling back to %s", name, raw, default)
        return default
    if int(raw) in _BLOCKED_PORTS:
        logger.warning("%s=%s is in the browser-blocked IRC range (6666-6669) — connections will fail", name, raw)
    return raw
