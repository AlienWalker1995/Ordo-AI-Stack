"""Client for the Hermes API server (Hermes v0.20.0 gateway/platforms/api_server.py).

Contract used, verified against that source:
  * auth: `Authorization: Bearer <API_SERVER_KEY>` on every /v1 route (GET /health is open)
  * POST /v1/chat/completions, non-streaming. A `system` message becomes an ephemeral system prompt
    layered on Hermes's own; the LAST user message is the turn's input.
  * `X-Hermes-Session-Id: <id>` pins the session. Without it Hermes derives the id from a hash of
    (system prompt, first user message), so two runs asking the same question would share one
    session and its accumulated history. The runner sends a unique id per (run, suite, item), which
    is also how trajectory metrics find the item's rows in state.db. Hermes echoes the id it used in
    the `X-Hermes-Session-Id` response header.
  * `X-Hermes-Session-Key: <key>` scopes long-term memory; the runner sends one key per run so eval
    turns never share a memory scope with the operator's channels.
  * failure shapes: 502 `{"error": {..., "code": "agent_incomplete"}}` when the agent produced no
    text; 200 with a `hermes` block when a run was partial/failed but produced some text; a client
    timeout (E10 round-4 fix: `HermesTurn.error_kind == "timeout"`) when the client gave up waiting -
    the session may still be alive in Hermes's state.db, so callers recover it there rather than
    discarding the item as an infra error (see suites/harness.py's call_hermes).
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any

import httpx


@dataclasses.dataclass
class HermesTurn:
    status_code: int | None
    text: str | None
    session_id: str
    wall_time_s: float
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    hermes: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    # "transport": the runner never got an answer from Hermes (connection refused, 401, 5xx);
    #              excluded from harness quality metrics and counted as an infra error.
    # "timeout":   the client gave up waiting (a per-item wall-clock budget, or the transport's own
    #              timeout - see suites/harness.py's call_hermes, E10 round-4 fix). NOT an infra
    #              error: the caller recovers the session from Hermes's state.db by session_id and
    #              scores it a real result (did_not_converge), because the agent was often still
    #              alive and had reached an answer.
    # "agent":     Hermes answered with a failure; that IS a harness result.
    error_kind: str | None = None
    # E10: True when error_kind == "timeout" - the client stopped waiting rather than Hermes ever
    # reporting back. Carried through to the item's metadata for visibility, alongside wall_time_s
    # (the elapsed time at which the client gave up).
    budget_exceeded: bool = False


class HermesClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float):
        if not api_key:
            raise ValueError("HERMES_API_SERVER_KEY is empty: the harness suites cannot authenticate")
        self._base_url = base_url.rstrip("/")
        self._root_url = self._base_url.removesuffix("/v1")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = httpx.Timeout(timeout_s, connect=15.0)

    async def version(self) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self._root_url}/health")
            response.raise_for_status()
            return str(response.json().get("version", "unknown"))

    async def model_id(self) -> str:
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as client:
            response = await client.get(f"{self._base_url}/models")
            response.raise_for_status()
            return str(response.json()["data"][0]["id"])

    async def chat(self, *, prompt: str, system: str | None, session_id: str, session_key: str,
                   model: str) -> HermesTurn:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        headers = dict(self._headers, **{"X-Hermes-Session-Id": session_id, "X-Hermes-Session-Key": session_key})
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
                response = await client.post(f"{self._base_url}/chat/completions",
                                             json={"model": model, "messages": messages, "stream": False})
        except httpx.TimeoutException as exc:
            # E10: a timeout is not "transport unreachable" - Hermes may well still be working the
            # turn. Kept distinct so the caller (suites/harness.py's call_hermes) always attempts a
            # state.db recovery for it, unlike a genuine transport failure below.
            return HermesTurn(None, None, session_id, round(time.monotonic() - started, 3),
                              error=f"{type(exc).__name__}: {exc}", error_kind="timeout", budget_exceeded=True)
        except httpx.HTTPError as exc:
            return HermesTurn(None, None, session_id, round(time.monotonic() - started, 3),
                              error=f"{type(exc).__name__}: {exc}", error_kind="transport")
        elapsed = round(time.monotonic() - started, 3)
        used_session = response.headers.get("X-Hermes-Session-Id", session_id)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        status = response.status_code
        if status != 200:
            error = body.get("error") or {}
            # 500 (the agent run raised) and 502 (the agent produced no text) mean Hermes took the turn
            # and failed it: a harness result. Everything else (401 bad key, 404, 429 busy, 503
            # draining) means the turn never ran: an infra error.
            kind = "agent" if status in (500, 502) else "transport"
            return HermesTurn(status, None, used_session, elapsed, hermes=error.get("hermes") or {},
                              error=error.get("message") or f"HTTP {status}", error_kind=kind)
        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content")
        return HermesTurn(200, text, used_session, elapsed, usage=body.get("usage") or {},
                          hermes=body.get("hermes") or {},
                          error=(body.get("hermes") or {}).get("error"),
                          error_kind="agent" if body.get("hermes") else None)
