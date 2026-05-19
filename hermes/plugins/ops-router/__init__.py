"""ops-router — Hermes plugin exposing ops-controller verbs as first-class tools.

Replaces the lost docker.sock surface (Plan C). Five tools wrap ops-controller's
HTTP API so the model never has to know about curl or HTTP:

- list_containers    -> GET  /containers
- container_logs     -> GET  /containers/{name}/logs
- restart_container  -> POST /containers/{name}/restart       (bounce existing container)
- compose_restart    -> POST /compose/restart                 (compose-aware restart, same env)
- compose_up         -> POST /compose/up                      (recreate; picks up new .env / volumes / network)

When to use which:
- Process is wedged or a bind-mounted file changed   -> restart_container / compose_restart
- .env, image, volumes, or network changed           -> compose_up (recreate)

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


def _compose_restart(args: dict, **kwargs) -> str:
    service = (args.get("service") or "").strip() or None
    confirm = bool(args.get("confirm"))
    if service is None and not confirm:
        return _err("whole-stack restart requires confirm=true; pass a service name to scope")
    try:
        result = _get_client().compose_restart(service=service, confirm=confirm)
        return json.dumps({"ok": True, **result})
    except OpsClientError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.exception("compose_restart failed")
        return _err(f"unexpected error: {exc}")


def _compose_up(args: dict, **kwargs) -> str:
    service = (args.get("service") or "").strip() or None
    confirm = bool(args.get("confirm"))
    if service is None and not confirm:
        return _err("whole-stack up requires confirm=true; pass a service name to scope")
    try:
        result = _get_client().compose_up(service=service, confirm=confirm)
        return json.dumps({"ok": True, **result})
    except OpsClientError as exc:
        return _err(str(exc))
    except Exception as exc:
        logger.exception("compose_up failed")
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
        "Bounce a single container by name via ops-controller's "
        "/containers/{name}/restart endpoint. Works for ANY container the host "
        "daemon sees (including non-Ordo containers like `min-max-web-dev-1`). "
        "Use this INSTEAD of `terminal: docker restart ...` — Hermes has no "
        "docker socket; that command will always fail. "
        "NOTE: this does NOT pick up changes to environment variables / .env / "
        "volumes — use `compose_up` for that."
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

COMPOSE_RESTART_SCHEMA = {
    "name": "compose_restart",
    "description": (
        "Compose-aware restart: `docker compose restart <service>` via "
        "ops-controller's /compose/restart endpoint. Bounces the process but "
        "does NOT recreate the container — does NOT pick up .env or compose "
        "config changes. Use `compose_up` for those. Use this when the process "
        "is wedged and you want a clean restart of the existing container."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": (
                    "Compose service name (e.g. `llamacpp`, `hermes-gateway`). "
                    "Omit to restart the whole stack — requires confirm=true."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": "Required (true) when service is omitted. Guards against prompt-injected stack-wide restarts.",
            },
        },
        "required": [],
    },
}

COMPOSE_UP_SCHEMA = {
    "name": "compose_up",
    "description": (
        "Compose recreate: `docker compose up -d <service>` via ops-controller's "
        "/compose/up endpoint. Recreates the container so it picks up changes "
        "to .env / environment / volumes / network / image. This is the verb "
        "you want after editing .env (e.g. changing LLAMACPP_MODEL). Does NOT "
        "rebuild images; if you need a rebuild, ask the operator to run "
        "`docker compose up -d --build --force-recreate <service>` from the host."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": (
                    "Compose service name (e.g. `llamacpp`, `model-gateway`). "
                    "Omit to recreate the whole stack — requires confirm=true."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": "Required (true) when service is omitted. Guards against prompt-injected stack-wide recreates.",
            },
        },
        "required": [],
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
    "Use the first-class tools: `list_containers`, `container_logs(name, tail)`, "
    "`restart_container(name)`, `compose_restart(service)`, `compose_up(service)`. "
    "Picking the right verb: if .env / environment / volumes changed, use "
    "`compose_up(service=...)` (recreate) — `restart_container` and "
    "`compose_restart` only bounce the existing container and will NOT pick up "
    "env changes. The OPS_CONTROLLER_TOKEN is already in your env — do NOT "
    "generate a new one or write tokens to .env. For deeper context, load the "
    "`devops/ops-controller-api` skill."
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
        description="Bounce a single container by name (does NOT pick up env changes).",
        emoji="🔁",
    )
    ctx.register_tool(
        name="compose_restart",
        toolset="ops-router",
        schema=COMPOSE_RESTART_SCHEMA,
        handler=_compose_restart,
        description="Compose-aware restart of a service (does NOT pick up env changes).",
        emoji="🔄",
    )
    ctx.register_tool(
        name="compose_up",
        toolset="ops-router",
        schema=COMPOSE_UP_SCHEMA,
        handler=_compose_up,
        description="Compose recreate: applies .env / volume / network changes.",
        emoji="⬆️",
    )
    ctx.register_hook("pre_llm_call", _intent_nudge)
