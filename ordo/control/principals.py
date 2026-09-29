"""Who may call which ops-controller route.

A principal is what a bearer token proves: a name, the token that identifies it, and the routes
it may call. The admin principal (OPS_CONTROLLER_TOKEN) may call every route, as every caller
could before principals existed. The hermes principal (OPS_CONTROLLER_TOKEN_HERMES) may call only
the routes in HERMES_ROUTES: observation, the GPU lease, and per-service recovery of the Ordo
project (hostile audit SEC-1). Hermes reads untrusted input (Discord messages, web pages, MCP
tool output), so its token must not carry stack-wide or code-executing verbs.

The allowlist is code, baked into the ops-controller image, on purpose: a rendered file under
out/ would be writable by anything that can write the render output.

The audit log names the principal a call proved (`principal`) beside the name the caller chose
to send in X-Actor (`caller`). Only the first is evidence.
"""
from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

ADMIN = "admin"
HERMES = "hermes"
# The audit name of a call that proved no principal (no token, or a wrong one).
UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class Route:
    """One allowed (method, path template) pair. `{name}` in a template matches exactly one path
    segment, so `/services/{id}/restart` never matches `/services/a/b/restart`."""

    method: str
    template: str

    def matches(self, method: str, path: str) -> bool:
        return method.upper() == self.method and _template_regex(self.template).fullmatch(path) is not None


def _template_regex(template: str) -> re.Pattern[str]:
    parts = []
    for segment in template.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    return re.compile("/".join(parts))


# Every route the hermes principal may call. Anything else, including a path nothing serves, is
# refused with 403 before routing. Adding a route here is a privilege grant: review it as one.
HERMES_ROUTES: tuple[Route, ...] = (
    # Observation.
    Route("GET", "/status"),
    Route("GET", "/containers"),
    Route("GET", "/containers/{name}/logs"),
    Route("GET", "/containers/{name}"),   # field-allowlisted inspect: no environment, no labels
    Route("GET", "/services"),
    Route("GET", "/services/{id}/logs"),
    Route("GET", "/stats/services"),
    Route("GET", "/plugins"),
    Route("GET", "/model-config"),
    Route("GET", "/gpus"),
    Route("GET", "/registry/models"),
    Route("GET", "/registry/gpus"),
    Route("GET", "/jobs/history"),
    Route("GET", "/diagnostics/dstate"),
    Route("GET", "/models/download/status"),
    # The GPU lease: the only sanctioned way onto the card.
    Route("POST", "/jobs"),
    Route("POST", "/jobs/heartbeat"),
    Route("POST", "/jobs/complete"),
    # Per-service recovery of the Ordo project. Lease-checked and confirm-gated by the routes
    # themselves; the agent and ops-controller refuse to cycle themselves.
    Route("POST", "/services/{id}/restart"),
    Route("POST", "/services/{id}/recreate"),
    Route("POST", "/containers/{name}/restart"),
    # Source writes that already exist as Hermes tools (confirm-gated, render-checked).
    Route("POST", "/plugins/{id}/enable"),
    Route("POST", "/plugins/{id}/disable"),
    Route("POST", "/model-config"),
    Route("POST", "/models/download"),
    # OTHER compose projects the operator listed in `managed_projects:` (ordo/control/managed.py):
    # status, logs and a confirmed, rate-limited, GPU-guarded restart. Nothing else.
    Route("GET", "/projects"),
    Route("GET", "/projects/{project}/containers"),
    Route("GET", "/projects/{project}/containers/{name}/logs"),
    Route("POST", "/projects/{project}/containers/{name}/restart"),
)


class TokenSource:
    """A token read on every use, so a rotated file takes effect without a restart.

    A read that FAILS (a file mid-replace, a transient mount error) keeps the last good value.
    A read that succeeds EMPTY depends on who the token is for:
    - the admin token keeps the last good value (`empty_revokes=False`): a torn write must never
      lock every caller out of the control plane (#290);
    - a scoped token is revoked at once (`empty_revokes=True`): emptying its file is how an
      operator turns the principal off, and a store without the key materializes an empty file.
      A torn write then fails closed for one request, never open.
    A source that has never produced a value yields "" and matches nothing."""

    def __init__(self, read: Callable[[], str], *, empty_revokes: bool = False):
        self._read = read
        self._empty_revokes = empty_revokes
        self._last_good = ""

    def current(self) -> str:
        try:
            token = (self._read() or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable file keeps the last good token
            return self._last_good
        if token or self._empty_revokes:
            self._last_good = token
        return self._last_good


@dataclass(frozen=True)
class Principal:
    name: str
    token: TokenSource
    # None: every route (the admin principal). Otherwise exactly these.
    routes: tuple[Route, ...] | None

    def allows(self, method: str, path: str) -> bool:
        if self.routes is None:
            return True
        return any(route.matches(method, path) for route in self.routes)


def admin(read_token: Callable[[], str]) -> Principal:
    return Principal(ADMIN, TokenSource(read_token), None)


def hermes(read_token: Callable[[], str]) -> Principal:
    return Principal(HERMES, TokenSource(read_token, empty_revokes=True), HERMES_ROUTES)


def authenticate(principals: Sequence[Principal], authorization: str) -> Principal | None:
    """The principal whose token the `Authorization` header presents, or None.

    Only the scheme's case is normalised; the token is compared exactly and in constant time, and
    every principal is compared, so the time taken does not say which one matched. The first match
    wins: principals are ordered admin first, so a scoped token that equals the admin token (a
    misconfigured store) is the admin credential it is, never a narrower one."""
    scheme, _, token = (authorization or "").partition(" ")
    presented = f"{scheme.lower()} {token.strip()}".encode()
    matched: Principal | None = None
    for principal in principals:
        expected = principal.token.current()
        if not expected:
            continue
        if hmac.compare_digest(presented, f"bearer {expected}".encode()) and matched is None:
            matched = principal
    return matched
