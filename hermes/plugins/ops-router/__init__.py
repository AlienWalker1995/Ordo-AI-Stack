"""ops-router — Hermes plugin exposing ops-controller verbs as first-class tools.

Replaces the lost docker.sock surface (Plan C). Three tools wrap ops-controller's
HTTP API so the model never has to know about curl or HTTP:

- list_containers    -> GET  /containers
- container_logs     -> GET  /containers/{name}/logs
- restart_container  -> POST /containers/{name}/restart

Plus a pre_llm_call hook that nudges the model toward these tools when the user
message contains docker / container / restart / logs intent — guards against
the model defaulting to `terminal: docker ...` (which has no socket and always
fails with "Cannot connect to the Docker daemon").

The OpsClient is the canonical hermes/ops_client.py copied into this plugin
directory at Docker build time (see hermes/Dockerfile).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .ops_client import OpsClient, OpsClientError

logger = logging.getLogger(__name__)

# Lazy singleton — constructed on first tool call. If OPS_CONTROLLER_TOKEN
# is unset the constructor raises; we surface that as a tool-result error
# instead of crashing the plugin at register time.
_client: OpsClient | None = None


def _get_client() -> OpsClient:
    global _client
    if _client is None:
        _client = OpsClient()
    return _client


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _list_containers(args: dict, **kwargs) -> str:
    try:
        return json.dumps({"ok": True, "containers": _get_client().list_containers()})
    except OpsClientError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.exception("list_containers failed")
        return _err(f"unexpected error: {exc}")


def _container_logs(args: dict, **kwargs) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        tail = int(args.get("tail") or 100)
    except (TypeError, ValueError):
        return _err("tail must be an integer")
    since = args.get("since") or None
    try:
        text = _get_client().container_logs(name, tail=tail, since=since)
        return json.dumps({"ok": True, "name": name, "tail": tail, "logs": text})
    except OpsClientError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.exception("container_logs failed")
        return _err(f"unexpected error: {exc}")


def _restart_container(args: dict, **kwargs) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        result = _get_client().restart_container(name)
        return json.dumps({"ok": True, **result})
    except OpsClientError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.exception("restart_container failed")
        return _err(f"unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Tool schemas — descriptions are how the model decides when to use these
# ---------------------------------------------------------------------------

LIST_CONTAINERS_SCHEMA = {
    "name": "list_containers",
    "description": (
        "List every Docker container visible to the host daemon (every compose "
        "project, not just Ordo). Returns name, status, image. "
        "Use this INSTEAD of `terminal: docker ps` — Hermes has no docker socket."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CONTAINER_LOGS_SCHEMA = {
    "name": "container_logs",
    "description": (
        "Tail a container's logs by name. Works for ANY container on the host "
        "daemon, not just Ordo-allowlisted services (e.g. `min-max-web-dev-1`). "
        "Use this INSTEAD of `terminal: docker logs ...`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Container name, e.g. 'min-max-web-dev-1' or 'comfyui'.",
            },
            "tail": {
                "type": "integer",
                "description": "Number of trailing log lines to return. Default 100.",
            },
            "since": {
                "type": "string",
                "description": (
                    "Optional Docker `since` filter — duration like '10m' or RFC3339 "
                    "timestamp like '2026-05-09T10:00:00'."
                ),
            },
        },
        "required": ["name"],
    },
}

RESTART_CONTAINER_SCHEMA = {
    "name": "restart_container",
    "description": (
        "Restart any Docker container by name via ops-controller's "
        "/containers/{name}/restart endpoint. Works for ANY container the host "
        "daemon sees (including non-Ordo containers like `min-max-web-dev-1`). "
        "Use this INSTEAD of `terminal: docker restart ...` — Hermes has no "
        "docker socket; that command will always fail."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Container name to restart.",
            },
        },
        "required": ["name"],
    },
}


# ---------------------------------------------------------------------------
# pre_llm_call intent nudge
# ---------------------------------------------------------------------------

# Verb-style mentions only — avoids firing on prose that merely mentions a
# container in passing ("the container ship"). High-recall regex; the
# tradeoff is one extra ~120-token nudge per matching turn.
_DOCKER_INTENT = re.compile(
    r"\b(?:"
    r"docker(?:\s+(?:compose|restart|logs|ps|exec|stop|start|inspect|run|kill|pull|build|up|down))?"
    r"|restart\s+(?:it|this|that|the)\b"
    r"|restart\s+\w+[-_]\w+"
    r"|(?:tail|view|show|check|fetch|grab)\s+(?:the\s+)?(?:\w+\s+)?logs?\b"
    r"|stop\s+(?:the\s+)?\w+\s+container"
    r"|bring\s+(?:up|down)\b"
    r"|compose\s+(?:up|down|restart)"
    r"|service\s+(?:up|down|restart)"
    r"|container\s+(?:up|down|restart)"
    r")\b",
    re.IGNORECASE,
)

_NUDGE = (
    "Routing note: this turn looks like a docker/container op. Hermes has no "
    "docker socket — DO NOT call `terminal` or `execute_code` with `docker ...`; "
    "it will fail with 'Cannot connect to the Docker daemon'. "
    "Use the first-class tools instead: `list_containers`, "
    "`container_logs(name, tail)`, `restart_container(name)`. "
    "They route via ops-controller and work on ANY container the host daemon "
    "sees, including non-Ordo containers like `min-max-web-dev-1`. "
    "For whole-stack compose ops (up/down/restart), follow the "
    "`devops/ops-controller-api` skill — load it now if you haven't."
)


def _intent_nudge(user_message: str = "", **kwargs: Any) -> dict | None:
    if not isinstance(user_message, str) or not user_message:
        return None
    if _DOCKER_INTENT.search(user_message):
        return {"context": _NUDGE}
    return None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_tool(
        name="list_containers",
        toolset="ops-router",
        schema=LIST_CONTAINERS_SCHEMA,
        handler=_list_containers,
        description="List all containers via ops-controller (no docker socket needed).",
        emoji="🧱",
    )
    ctx.register_tool(
        name="container_logs",
        toolset="ops-router",
        schema=CONTAINER_LOGS_SCHEMA,
        handler=_container_logs,
        description="Tail any container's logs via ops-controller.",
        emoji="📜",
    )
    ctx.register_tool(
        name="restart_container",
        toolset="ops-router",
        schema=RESTART_CONTAINER_SCHEMA,
        handler=_restart_container,
        description="Restart any container by name via ops-controller.",
        emoji="🔁",
    )
    ctx.register_hook("pre_llm_call", _intent_nudge)
