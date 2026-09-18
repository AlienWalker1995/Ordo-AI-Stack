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
