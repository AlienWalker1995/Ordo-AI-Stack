"""Dependency failure diagnostics for the dashboard.

Every outbound call the dashboard makes to a dependency (ops-controller, Prometheus, Grafana,
ComfyUI, the llama.cpp servers, Qdrant, ...) degrades instead of 500ing when it fails. This
module gives each failure a stable code (DEP-<DEP>-<KIND>), ties it to the request's trace id,
logs exactly one WARNING line, and keeps an in-memory ring of recent failures plus per-dependency
status so GET /api/diagnostics can answer what is failing and since when.

Details are short by contract: callers pass a one-line summary without tokens, headers or bodies;
record_failure truncates to _DETAIL_MAX characters. The ring and the status map are process-local
(the dashboard runs as a single uvicorn process, like the throughput store in app.py).
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from contextvars import ContextVar

import httpx

logger = logging.getLogger(__name__)

# The DEP part of a code: which dependency failed. Instrumented today: OPS (_ops_request) and
# PROMETHEUS, GRAFANA, COMFYUI, LLAMACPP (_get_json). The rest are reserved for the remaining
# outbound helpers (slice 2) and read `unknown` in snapshot() until something records them.
DEPENDENCIES = ("OPS", "GATEWAY", "PROMETHEUS", "GRAFANA", "COMFYUI", "LLAMACPP", "QDRANT", "EDGE", "PROBE", "FS")

# The KIND part of a code, and what each means. TIMEOUT, UNREACHABLE, HTTP5XX, HTTP4XX and AUTH
# come from classify_failure; BADPAYLOAD and NOTCONFIGURED are declared by the caller (an
# unparseable body, a missing URL or token).
KIND_MEANINGS = {
    "UNREACHABLE": "the connection failed before a response (refused, DNS, protocol error)",
    "TIMEOUT": "the dependency did not answer within the client's timeout",
    "HTTP5XX": "the dependency answered with a 5xx status",
    "HTTP4XX": "the dependency answered with a 4xx status",
    "BADPAYLOAD": "the response body could not be parsed as JSON",
    "AUTH": "the dependency rejected the credentials (HTTP 401/403)",
    "NOTCONFIGURED": "the dependency's URL or token is not set",
}

_RING_CAP = 200
_DETAIL_MAX = 200

# str(exc) of an httpx error embeds the full request URL (hostname, port, path, query), usually
# inside parentheses: "Connection refused (GET http://ops-controller:9000/stats/services)". The
# ring is served by the open GET /api/diagnostics, so strip URLs before anything enters it; the
# match stops at whitespace, quotes and closing brackets so surrounding text stays balanced.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"\)\]\}>]+")


def sanitize_detail(detail: str) -> str:
    """Strip URLs from a failure detail: the ring is open-read, and exception text embeds internal
    hostnames, ports and paths."""
    return _URL_RE.sub("<url>", detail)

_trace_id: ContextVar[str] = ContextVar("dashboard_trace_id", default="")
_ring_lock = threading.Lock()
_failures: list[dict] = []   # the ring, oldest first
_dep_status: dict[str, dict] = {}


def new_trace_id() -> str:
    """A fresh trace id for a request the edge did not tag."""
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    """This request's trace id, or '-' outside a request (background work)."""
    return _trace_id.get() or "-"


def set_trace_id(trace: str):
    """Bind the request's trace id to this context; returns the token reset_trace_id takes back."""
    return _trace_id.set(trace)


def reset_trace_id(token) -> None:
    _trace_id.reset(token)


def classify_failure(exc: BaseException | None = None, *, status: int | None = None) -> str:
    """Map an httpx exception or a response status code to a failure KIND.

    Mapping (checked in this order):
      exception                                                    kind
      httpx.TimeoutException (ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout)
                                                                   TIMEOUT
      httpx.ConnectError                                           UNREACHABLE
      any other httpx.RequestError                                 UNREACHABLE
      any other exception                                          UNREACHABLE
      no exception, status given:
        >= 500                                                     HTTP5XX
        401 or 403                                                 AUTH
        400..499                                                   HTTP4XX
    ConnectTimeout subclasses both TimeoutException and ConnectError; the timeout reading wins.
    A status below 400 is a success, not a failure, so it raises.
    """
    if exc is not None:
        if isinstance(exc, httpx.TimeoutException):
            return "TIMEOUT"
        return "UNREACHABLE"
    if status is None:
        raise ValueError("classify_failure needs an exception or a status")
    if status >= 500:
        return "HTTP5XX"
    if status in (401, 403):
        return "AUTH"
    if 400 <= status < 500:
        return "HTTP4XX"
    raise ValueError(f"classify_failure: no failure kind for status {status}")


def record_failure(dep: str, kind: str, detail: str, *, target: str, elapsed_ms: float) -> str:
    """Log ONE WARNING line for a dependency failure and record it in the ring. Returns the code.

    detail must be a short one-liner without tokens, headers or bodies; URLs are stripped
    (sanitize_detail) and the result truncated to _DETAIL_MAX characters.
    """
    code = f"DEP-{dep}-{kind}"
    trace = current_trace_id()
    detail = sanitize_detail(detail)[:_DETAIL_MAX]
    logger.warning(
        "DEP_FAIL code=%s trace=%s target=%s elapsed_ms=%.0f detail=%s",
        code, trace, target, elapsed_ms, detail,
    )
    now = time.time()
    with _ring_lock:
        _failures.append({
            "at": now, "code": code, "dep": dep, "kind": kind, "trace": trace,
            "target": target, "elapsed_ms": round(elapsed_ms, 1), "detail": detail,
        })
        if len(_failures) > _RING_CAP:
            del _failures[: len(_failures) - _RING_CAP]
        st = _dep_status.setdefault(dep, {"last_ok": None, "last_failure": None, "consecutive_failures": 0})
        st["last_failure"] = {"code": code, "at": now, "detail": detail}
        st["consecutive_failures"] += 1
    return code


def mark_ok(dep: str) -> None:
    """Record a successful call: notes the last ok time and resets the consecutive-failure count."""
    with _ring_lock:
        st = _dep_status.setdefault(dep, {"last_ok": None, "last_failure": None, "consecutive_failures": 0})
        st["last_ok"] = time.time()
        st["consecutive_failures"] = 0


def snapshot() -> dict:
    """The GET /api/diagnostics payload: per-dependency status, the ring newest first, the codes.

    A dependency reads failing while its newest event is a failure, ok once it has answered since,
    unknown until anything has been observed.
    """
    with _ring_lock:
        dependencies = []
        for dep in DEPENDENCIES:
            st = _dep_status.get(dep)
            if st is None:
                status = "unknown"
            elif st["last_failure"] is not None and (st["last_ok"] is None or st["last_failure"]["at"] >= st["last_ok"]):
                status = "failing"
            else:
                status = "ok"
            dependencies.append({
                "dep": dep,
                "status": status,
                "last_ok": st["last_ok"] if st else None,
                "last_failure": dict(st["last_failure"]) if st and st["last_failure"] else None,
                "consecutive_failures": st["consecutive_failures"] if st else 0,
            })
        recent = [dict(entry) for entry in reversed(_failures)]
    codes = [{"code": f"DEP-{dep}-{kind}", "meaning": meaning}
             for dep in DEPENDENCIES for kind, meaning in KIND_MEANINGS.items()]
    return {
        "generated_at": time.time(),
        "dependencies": dependencies,
        "recent": recent,
        "codes": codes,
    }
