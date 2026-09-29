"""Where the Ordo checkout appears inside the agent, so its secret store can be hidden there.

The agent mirror-mounts the operator's code root (`site: CODE_ROOT`, default `/c/dev`) at
MIRROR_ROOT (services/hermes/agent.yaml). When the Ordo checkout (`site: BASE_PATH`) sits under
that root, the agent can read the materialized secret store in it: `out/secrets.env` and
`out/secrets/`, which hold the admin OPS_CONTROLLER_TOKEN and every other secret value. The
render computes the checkout's path inside the agent as AGENT_CHECKOUT_PATH, and the manifest
shadows those two paths there (hostile audit SEC-1).

A checkout outside the code root is not visible to the agent at all; the shadows then land on
NOT_MIRRORED, a path nothing else uses, so the manifest needs no conditional.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# The agent's mirror-mount target (services/hermes/agent.yaml: `${CODE_ROOT:-/c/dev}:/c/dev`).
MIRROR_ROOT = "/c/dev"
# The shadow target for a checkout the mirror does not expose.
NOT_MIRRORED = "/opt/ordo-checkout-not-mirrored"

_DRIVE = re.compile(r"^([A-Za-z]):(/|$)")


def _host_posix(path: str) -> PurePosixPath | None:
    """A host path as one POSIX form: backslashes become slashes and `C:/x` becomes `/c/x` (the
    form Docker Desktop uses for Windows drives), so the two spellings compare equal."""
    text = (path or "").strip().replace("\\", "/")
    if not text:
        return None
    drive = _DRIVE.match(text)
    if drive:
        text = f"/{drive.group(1).lower()}/{text[drive.end():]}"
    if not text.startswith("/"):
        return None
    return PurePosixPath(text.rstrip("/") or "/")


def in_agent(host_path: str, code_root: str) -> str:
    """A host path's location inside the agent, or NOT_MIRRORED when the mirror mount does not
    expose it. An empty CODE_ROOT means the manifest's default, MIRROR_ROOT itself."""
    base = _host_posix(host_path)
    root = _host_posix(code_root) or PurePosixPath(MIRROR_ROOT)
    if base is None:
        return NOT_MIRRORED
    try:
        relative = base.relative_to(root)
    except ValueError:
        return NOT_MIRRORED
    return str(PurePosixPath(MIRROR_ROOT) / relative)


def checkout_in_agent(base_path: str, code_root: str) -> str:
    """The Ordo checkout's path inside the agent (AGENT_CHECKOUT_PATH)."""
    return in_agent(base_path, code_root)
