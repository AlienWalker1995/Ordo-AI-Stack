"""E10 (round-4 fix): HermesClient must classify a client-side timeout separately from a genuine
transport failure, so callers (suites/harness.py's call_hermes) know to attempt a state.db recovery
for the former but never for the latter. Mocked at the transport level with respx (already a pinned
test dependency, tests/requirements.txt) rather than touching HermesClient's constructor for
testability."""
from __future__ import annotations

import httpx
import respx
from ordo_evals.hermes_client import HermesClient

URL = "http://fake-hermes/v1/chat/completions"


def client() -> HermesClient:
    return HermesClient(base_url="http://fake-hermes/v1", api_key="test-key-0123456789", timeout_s=5.0)


@respx.mock
async def test_a_read_timeout_is_classified_timeout_not_transport():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    turn = await client().chat(prompt="hi", system=None, session_id="s1", session_key="k1", model="local-chat")
    assert turn.error_kind == "timeout"
    assert turn.budget_exceeded is True
    assert turn.text is None
    assert turn.session_id == "s1"  # the id the runner sent, even though nothing came back


@respx.mock
async def test_a_connect_error_is_classified_transport_not_timeout():
    respx.post(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    turn = await client().chat(prompt="hi", system=None, session_id="s1", session_key="k1", model="local-chat")
    assert turn.error_kind == "transport"
    assert turn.budget_exceeded is False


@respx.mock
async def test_a_normal_reply_is_unaffected():
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": "hello"}}], "usage": {}},
        headers={"X-Hermes-Session-Id": "s1"}))
    turn = await client().chat(prompt="hi", system=None, session_id="s1", session_key="k1", model="local-chat")
    assert turn.status_code == 200
    assert turn.text == "hello"
    assert turn.error_kind is None
    assert turn.budget_exceeded is False


# ── E23 (round-10 fix): the in-flight agent work Hermes reports ──────────────────

HEALTH_URL = "http://fake-hermes/health/detailed"


@respx.mock
async def test_active_agent_work_reads_the_gateways_own_in_flight_count():
    """`active_agents` is the gateway's `_active_work_count()` - running agents, cron jobs and
    API-server work - and a non-streaming chat-completions turn is counted for as long as it runs.
    That is how the harness sees an abandoned item still holding the single model slot."""
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok", "active_agents": 2}))
    assert await client().active_agent_work() == 2


@respx.mock
async def test_an_idle_agent_reports_zero():
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok", "active_agents": 0}))
    assert await client().active_agent_work() == 0


@respx.mock
async def test_an_unreachable_or_unreadable_health_endpoint_is_none_never_zero():
    """"Could not tell" must never read as "the slot is free": the caller waits on a real zero and
    records a note for anything else (hermes_turn.wait_for_agent_idle)."""
    respx.get(HEALTH_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    assert await client().active_agent_work() is None
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    assert await client().active_agent_work() is None
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert await client().active_agent_work() is None
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"active_agents": "lots"}))
    assert await client().active_agent_work() is None
    respx.get(HEALTH_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    assert await client().active_agent_work() is None
