"""The dashboard's live model work view: POST /api/work/events (from the model gateway's
throughput callback) and GET /api/work (dashboard/live_work.py, the routes in app.py)."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import live_work

T0 = 1_760_000_000.0


def _started(call_id: str, caller: str = "agent-key", deployment: str = "llamacpp", at: float = T0, **extra) -> dict:
    return {"event": "started", "call_id": call_id, "caller": caller, "model_group": "local-chat",
            "model": "chat-model.gguf", "deployment": deployment, "prompt_tokens": 1200,
            "fallback": False, "started_at": at, **extra}


def _ended(call_id: str, caller: str = "agent-key", deployment: str = "llamacpp", started: float = T0,
           ended: float = T0 + 4, outcome: str = "ok", **extra) -> dict:
    return {"event": "ended", "call_id": call_id, "caller": caller, "model_group": "local-chat",
            "model": "chat-model.gguf", "deployment": deployment, "prompt_tokens": 1180,
            "completion_tokens": 64, "outcome": outcome, "fallback": False,
            "started_at": started, "ended_at": ended, **extra}


# --- the store ---

def test_a_started_request_is_in_flight_until_it_ends():
    store = live_work.WorkStore()
    store.started(_started("a"), now=T0)
    snap = store.snapshot(now=T0 + 10)
    assert [r["call_id"] for r in snap["inflight"]] == ["a"]
    assert snap["inflight"][0]["running_s"] == 10.0
    assert snap["engines"]["llamacpp"]["inflight"] == 1
    assert snap["engines"]["llamacpp-cpu"]["inflight"] == 0

    store.ended(_ended("a", ended=T0 + 12), now=T0 + 12)
    snap = store.snapshot(now=T0 + 13)
    assert snap["inflight"] == []
    assert snap["engines"]["llamacpp"]["inflight"] == 0
    assert snap["recent"]["callers"][0]["requests"] == 1


def test_a_start_whose_end_never_comes_expires_after_the_ttl():
    store = live_work.WorkStore()
    store.started(_started("lost"), now=T0)
    assert store.snapshot(now=T0 + live_work.INFLIGHT_TTL_S - 1)["inflight"]
    assert store.snapshot(now=T0 + live_work.INFLIGHT_TTL_S + 1)["inflight"] == []


def test_a_start_that_arrives_after_its_own_end_is_not_left_in_flight():
    store = live_work.WorkStore()
    store.ended(_ended("fast", started=T0, ended=T0 + 0.2), now=T0 + 0.3)
    store.started(_started("fast", at=T0), now=T0 + 0.4)
    assert store.snapshot(now=T0 + 1)["inflight"] == []


def test_a_fallback_hop_under_the_same_call_id_is_in_flight_again():
    """A GPU error then the CPU fallback hop: two attempts, one call_id."""
    store = live_work.WorkStore()
    store.started(_started("c"), now=T0)
    store.ended(_ended("c", ended=T0 + 1, outcome="error", error="InternalServerError"), now=T0 + 1)
    store.started(_started("c", deployment="llamacpp-cpu", at=T0 + 1.1, fallback=True), now=T0 + 1.1)
    snap = store.snapshot(now=T0 + 5)
    assert [(r["deployment"], r["fallback"]) for r in snap["inflight"]] == [("llamacpp-cpu", True)]
    assert snap["engines"]["llamacpp-cpu"]["inflight"] == 1
    assert snap["recent"]["callers"][0]["errors"] == 1


def test_an_end_keeps_the_start_estimate_when_the_engine_sent_no_usage():
    store = live_work.WorkStore()
    store.started(_started("e"), now=T0)
    store.ended(_ended("e", prompt_tokens=None, completion_tokens=None, outcome="error"), now=T0 + 4)
    row = store.snapshot(now=T0 + 5)["recent"]["callers"][0]
    assert row["prompt_tokens"] == 1200
    assert row["completion_tokens"] == 0


def test_the_recent_window_counts_per_caller_with_a_median_latency():
    store = live_work.WorkStore()
    now = T0 + 1000
    for i, latency in enumerate((2.0, 4.0, 30.0)):
        store.ended(_ended(f"a{i}", started=now - 100, ended=now - 100 + latency), now=now)
    store.ended(_ended("b0", caller="chat-ui", started=now - 50, ended=now - 49, outcome="error"), now=now)
    store.ended(_ended("old", caller="batch-job", started=now - 900, ended=now - 800), now=now)

    recent = store.snapshot(now=now)["recent"]
    assert recent["window_s"] == 300
    by_caller = {row["caller"]: row for row in recent["callers"]}
    assert set(by_caller) == {"agent-key", "chat-ui"}  # batch-job ended before the window
    agent = by_caller["agent-key"]
    assert (agent["requests"], agent["errors"], agent["p50_latency_s"]) == (3, 0, 4.0)
    assert (agent["prompt_tokens"], agent["completion_tokens"]) == (3 * 1180, 3 * 64)
    assert agent["last_seen"] == now - 70
    assert by_caller["chat-ui"]["errors"] == 1
    assert recent["callers"][0]["caller"] == "agent-key"  # busiest first


def test_the_ring_is_bounded():
    store = live_work.WorkStore()
    for i in range(live_work.RECENT_MAX + 5):
        store.ended(_ended(f"r{i}", ended=T0), now=T0)
    assert store.snapshot(now=T0)["recent"]["callers"][0]["requests"] == live_work.RECENT_MAX


def test_slots_progress_sums_the_busy_slots():
    slots = [
        {"id": 0, "is_processing": True, "n_prompt_tokens": 8000, "n_prompt_tokens_processed": 2048,
         "n_prompt_tokens_cache": 512},
        {"id": 1, "is_processing": False, "n_prompt_tokens": 4096, "n_prompt_tokens_processed": 0,
         "n_prompt_tokens_cache": 0},
    ]
    assert live_work.slots_progress(slots) == {"processing": True, "prompt_tokens": 8000, "processed": 2048,
                                               "cached": 512}
    assert live_work.slots_progress([slots[1]])["processing"] is False
    assert live_work.slots_progress({"error": "not a list"}) is None


# --- the routes ---

@pytest.fixture
def dashboard(monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_work_store", live_work.WorkStore())
    monkeypatch.setattr(dashboard_app, "_cpu_slots_cache", {"checked": 0.0, "value": None})
    monkeypatch.delenv("THROUGHPUT_RECORD_TOKEN", raising=False)
    return dashboard_app


def _slots_client(monkeypatch, dashboard_app, *, json=None, error: Exception | None = None) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = json
    client = MagicMock()
    client.get = AsyncMock(side_effect=error) if error else AsyncMock(return_value=response)
    monkeypatch.setattr(dashboard_app, "_http_client", client)
    return client


def test_work_events_need_the_throughput_token_when_it_is_set(dashboard, monkeypatch):
    monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
    client = TestClient(dashboard.app)
    assert client.post("/api/work/events", json=_started("a", at=time.time())).status_code == 401
    assert client.post("/api/work/events", json=_started("a", at=time.time()),
                       headers={"X-Throughput-Token": "wrong"}).status_code == 401
    assert client.post("/api/work/events", json=_started("a", at=time.time()),
                       headers={"X-Throughput-Token": "tp-secret"}).status_code == 200
    assert [r["call_id"] for r in client.get("/api/work").json()["inflight"]] == ["a"]


def test_a_malformed_event_is_refused(dashboard):
    client = TestClient(dashboard.app)
    assert client.post("/api/work/events", json={"event": "paused", "call_id": "a"}).status_code == 422
    assert client.post("/api/work/events", json={"event": "started", "call_id": ""}).status_code == 422


def test_the_work_view_is_an_open_read(dashboard, monkeypatch):
    """No principal, like the other read-only /api/* views; idle engines are not probed."""
    slots = _slots_client(monkeypatch, dashboard, json=[])
    body = TestClient(dashboard.app).get("/api/work").json()
    assert body["inflight"] == []
    assert body["recent"] == {"window_s": 300, "callers": []}
    assert body["engines"] == {"llamacpp": {"inflight": 0, "prefill": None},
                               "llamacpp-cpu": {"inflight": 0, "prefill": None}}
    assert isinstance(body["generated_at"], float)
    slots.get.assert_not_awaited()


def test_the_cpu_engine_reports_its_prefill_while_the_fallback_serves(dashboard, monkeypatch):
    slots = _slots_client(monkeypatch, dashboard, json=[
        {"id": 0, "is_processing": True, "n_prompt_tokens": 9000, "n_prompt_tokens_processed": 4096,
         "n_prompt_tokens_cache": 1024}])
    client = TestClient(dashboard.app)
    client.post("/api/work/events", json=_started("cpu-1", deployment="llamacpp-cpu", at=time.time(), fallback=True))
    cpu = client.get("/api/work").json()["engines"]["llamacpp-cpu"]
    assert cpu == {"inflight": 1, "prefill": {"available": True, "processing": True, "prompt_tokens": 9000,
                                              "processed": 4096, "cached": 1024}}
    client.get("/api/work")
    slots.get.assert_awaited_once()  # the read is cached for 2 s
    assert slots.get.await_args.args[0].endswith("/slots")


def test_an_unreachable_cpu_engine_reads_as_unavailable(dashboard, monkeypatch):
    _slots_client(monkeypatch, dashboard, error=httpx.ConnectError("no route"))
    client = TestClient(dashboard.app)
    client.post("/api/work/events", json=_started("cpu-1", deployment="llamacpp-cpu", at=time.time()))
    response = client.get("/api/work")
    assert response.status_code == 200
    assert response.json()["engines"]["llamacpp-cpu"]["prefill"] == {
        "available": False, "processing": False, "prompt_tokens": 0, "processed": 0, "cached": 0}
