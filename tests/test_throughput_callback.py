"""Unit tests for the model-gateway throughput callback's deployment resolution.

Samples must key by the GGUF that ACTUALLY served the completion (the deployment's
model_info.weights_file, entrypoint-substituted from the same .env the llama-server
reads), never by the requested routing alias: `local-chat` conflates every model
ever active behind it, and CPU-failover completions (~10x slower) must attribute to
the CPU GGUF instead of polluting GPU percentiles.

litellm is not a test dependency (huge tree); the callback only needs its
CustomLogger base class, so the module chain is stubbed before the file loads."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]

_litellm = ModuleType("litellm")
_integrations = ModuleType("litellm.integrations")
_custom_logger = ModuleType("litellm.integrations.custom_logger")
_custom_logger.CustomLogger = object
_litellm.integrations = _integrations
_integrations.custom_logger = _custom_logger
sys.modules.setdefault("litellm", _litellm)
sys.modules.setdefault("litellm.integrations", _integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", _custom_logger)

_spec = importlib.util.spec_from_file_location(
    "throughput_callback_under_test",
    REPO / "services" / "model-gateway" / "throughput_callback.py",
)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

GPU_GGUF = "/models/Qwen-GPU-Q6_K.gguf"
CPU_GGUF = "/models/Qwen-CPU-A3B.gguf"


def _kwargs(weights: str | None, api_base: str, alias: str = "local-chat") -> dict:
    """Callback kwargs as the LiteLLM v1.82.3 router+logger assemble them:
    deployment model_info at top level AND under litellm_params.metadata."""
    mi = {"weights_file": weights} if weights else {}
    return {
        "model": f"openai/{alias}",
        "model_info": dict(mi),
        "litellm_params": {"metadata": {"model_info": dict(mi), "api_base": api_base}},
        "standard_logging_object": {
            "model_group": alias,
            "api_base": api_base,
            "user_agent": "python-httpx/0.27",
        },
    }


def _response(model: str = "local-chat", completion_tokens: int = 64, predicted_per_second: float | None = None):
    response = {"model": model, "usage": {"completion_tokens": completion_tokens, "prompt_tokens": 10}}
    if predicted_per_second is not None:
        response["timings"] = {"predicted_n": completion_tokens, "predicted_per_second": predicted_per_second}
    return response


def test_resolves_model_to_served_gguf_not_alias():
    kw = _kwargs(GPU_GGUF, "http://llamacpp:8080/v1")
    assert tc._resolve_model(kw, _response()) == "Qwen-GPU-Q6_K.gguf"


def test_cpu_failover_attributes_to_cpu_gguf_and_backend():
    kw = _kwargs(CPU_GGUF, "http://llamacpp-cpu:8080/v1")
    assert tc._resolve_model(kw, _response()) == "Qwen-CPU-A3B.gguf"
    assert tc._detect_backend(kw) == "llamacpp-cpu"
    assert tc._detect_alias(kw) == "local-chat"


def test_missing_router_info_falls_back_to_legacy_naming():
    """Telemetry must never be dropped when the router info is absent."""
    kw = {"model": "openai/local-chat", "standard_logging_object": {}}
    assert tc._resolve_model(kw, _response(model="local-chat")) == "local-chat"


def test_build_payload_includes_alias_and_backend():
    start = datetime(2026, 8, 17, 12, 0, 0)
    end = start + timedelta(seconds=2)
    kw = _kwargs(GPU_GGUF, "http://llamacpp:8080/v1")
    payload = tc._build_payload(kw, _response(completion_tokens=64, predicted_per_second=32.0), start, end)
    assert payload["model"] == "Qwen-GPU-Q6_K.gguf"
    assert payload["alias"] == "local-chat"
    assert payload["backend"] == "llamacpp"
    assert payload["output_tokens_per_sec"] == 32.0


def test_build_payload_still_none_without_completion_tokens():
    start = datetime(2026, 8, 17, 12, 0, 0)
    end = start + timedelta(seconds=2)
    kw = _kwargs(GPU_GGUF, "http://llamacpp:8080/v1")
    assert tc._build_payload(kw, _response(completion_tokens=0), start, end) is None


# --- generation speed: the engine's, not the whole request's ---

def test_a_queued_request_records_the_engines_speed_not_the_wait():
    """Live 2026-10-12: a 30-token reply waited ~4 minutes in NInfer's queue and then generated at
    206 tok/s. Dividing by the whole request reported 0.13 tok/s; the engine's timings say 206."""
    start = datetime(2026, 10, 12, 18, 0, 0)
    end = start + timedelta(seconds=239)
    kw = _kwargs(GPU_GGUF, "http://llamacpp:8080/v1")
    payload = tc._build_payload(kw, _response(completion_tokens=30, predicted_per_second=206.48), start, end)
    assert payload["output_tokens_per_sec"] == 206.48


def _chunk(content=None, reasoning=None, timings=None):
    """A ModelResponseStream-shaped chunk: one choice with a delta, timings on the final one."""
    from types import SimpleNamespace
    choices = [] if timings else [SimpleNamespace(delta=SimpleNamespace(
        role="assistant", content=content, reasoning_content=reasoning, tool_calls=None))]
    return SimpleNamespace(choices=choices, timings=timings)


def _stream_through_hook(call_id: str, chunks_at: list) -> list:
    """Run chunks through the recorder's iterator hook, stamping each with its arrival time."""
    import asyncio

    clock = iter([at for at, _ in chunks_at])

    async def source():
        for _, chunk in chunks_at:
            yield chunk

    async def drain():
        hook = tc.ThroughputRecorder().async_post_call_streaming_iterator_hook(
            None, source(), {"litellm_call_id": call_id})
        return [chunk async for chunk in hook]

    real_now = tc.datetime
    class _Clock(real_now):
        @classmethod
        def now(cls, tz=None):
            return next(clock)
    tc.datetime = _Clock
    try:
        return asyncio.run(drain())
    finally:
        tc.datetime = real_now


def test_a_streamed_reply_uses_the_final_chunks_engine_timings():
    """Live NInfer stream, 2026-10-12: an empty role chunk at once, 312 s queued, then tokens, then
    a final chunk carrying timings (217 tok/s). The gateway's assembled response drops timings."""
    t0 = datetime(2026, 10, 12, 19, 0, 0)
    chunks = [(t0, _chunk(content="")), (t0 + timedelta(seconds=312), _chunk(content="1")),
              (t0 + timedelta(seconds=312.3), _chunk(timings={"predicted_per_second": 217.15}))]
    passed = _stream_through_hook("call-timed", chunks)
    assert passed == [c for _, c in chunks]                  # every chunk passes through unchanged
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "litellm_call_id": "call-timed",
          "completion_start_time": t0}
    payload = tc._build_payload(kw, _response(completion_tokens=60), t0, t0 + timedelta(seconds=312.3))
    assert payload["output_tokens_per_sec"] == 217.15


def test_a_streamed_reply_without_timings_counts_from_the_first_token_bearing_chunk():
    """The empty role chunk sent at queue time must not start the clock."""
    t0 = datetime(2026, 10, 12, 19, 0, 0)
    chunks = [(t0, _chunk(content="")), (t0 + timedelta(seconds=300), _chunk(reasoning="Let me")),
              (t0 + timedelta(seconds=305), _chunk(content="done"))]
    _stream_through_hook("call-untimed", chunks)
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "litellm_call_id": "call-untimed",
          "completion_start_time": t0}
    end = t0 + timedelta(seconds=310)
    payload = tc._build_payload(kw, _response(completion_tokens=1000), t0, end)
    assert payload["output_tokens_per_sec"] == 100.0                 # 1000 tokens over 10 s, not 310


def test_observations_are_bounded():
    for i in range(tc._MAX_STREAM_OBSERVATIONS + 5):
        tc._observe_stream_chunk(f"call-{i}", _chunk(content="x"), datetime(2026, 10, 12))
    assert len(tc._STREAM_OBSERVATIONS) == tc._MAX_STREAM_OBSERVATIONS
    tc._STREAM_OBSERVATIONS.clear()


def test_no_sample_when_generation_time_is_unknown():
    """No engine timings and no observed stream: the only clock left (completion_start_time, the
    first chunk of any kind) includes the queue, so skip it."""
    start = datetime(2026, 10, 12, 18, 0, 0)
    end = start + timedelta(seconds=239)
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "completion_start_time": start}
    assert tc._build_payload(kw, _response(completion_tokens=30), start, end) is None


def test_a_streamed_reply_with_response_timings_does_not_leak_its_observation():
    """The response's own timings win, but the stream's observation must still be taken out:
    otherwise every such reply lingers in the bounded map until evicted, shrinking the budget
    for the untimed streams this exists to measure."""
    t0 = datetime(2026, 10, 12, 19, 0, 0)
    chunks = [(t0, _chunk(content="1")),
              (t0 + timedelta(seconds=2), _chunk(timings={"predicted_per_second": 217.15}))]
    _stream_through_hook("call-leak", chunks)
    assert "call-leak" in tc._STREAM_OBSERVATIONS
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "litellm_call_id": "call-leak",
          "completion_start_time": t0}
    payload = tc._build_payload(kw, _response(completion_tokens=60, predicted_per_second=180.0),
                                t0, t0 + timedelta(seconds=2))
    assert payload["output_tokens_per_sec"] == 180.0      # the response's timings win
    assert "call-leak" not in tc._STREAM_OBSERVATIONS     # ...and the observation is gone


def test_a_tool_call_only_chunk_starts_the_stream_clock():
    """A reply made entirely of tool calls carries no content or reasoning; its first
    tool-call chunk is still generated output and must start the clock."""
    from types import SimpleNamespace
    tool_chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        role="assistant", content=None, reasoning_content=None,
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "search", "arguments": ""}}]))])
    assert tc._chunk_carries_tokens(tool_chunk)
    t0 = datetime(2026, 10, 12, 19, 0, 0)
    chunks = [(t0, _chunk(content="")), (t0 + timedelta(seconds=400), tool_chunk)]
    _stream_through_hook("call-tool", chunks)
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "litellm_call_id": "call-tool",
          "completion_start_time": t0}
    end = t0 + timedelta(seconds=410)
    payload = tc._build_payload(kw, _response(completion_tokens=40), t0, end)
    assert payload["output_tokens_per_sec"] == 4.0        # 40 tokens over 10 s, not 410
    tc._STREAM_OBSERVATIONS.clear()


def test_an_error_in_the_stream_passes_through_to_the_caller():
    """The hook wraps the source iterator: an exception raised mid-stream must reach the caller
    unchanged (telemetry must never swallow a failed completion), and the chunks before it were
    observed."""
    import asyncio

    async def source():
        yield _chunk(content="x")
        raise RuntimeError("boom")

    async def drain():
        hook = tc.ThroughputRecorder().async_post_call_streaming_iterator_hook(
            None, source(), {"litellm_call_id": "call-err"})
        try:
            [chunk async for chunk in hook]
        except RuntimeError as exc:
            return exc
        return None

    exc = asyncio.run(drain())
    assert isinstance(exc, RuntimeError) and str(exc) == "boom"
    assert "first_token" in tc._STREAM_OBSERVATIONS.get("call-err", {})
    tc._STREAM_OBSERVATIONS.clear()


def test_stopping_early_closes_the_source_without_error():
    """A consumer that breaks out of the stream closes the wrapper; the close must propagate to
    the source (its cleanup runs) without raising."""
    import asyncio

    closed = []

    async def source():
        try:
            yield _chunk(content="x")
            yield _chunk(content="y")
        finally:
            closed.append(True)

    async def drain():
        hook = tc.ThroughputRecorder().async_post_call_streaming_iterator_hook(
            None, source(), {"litellm_call_id": "call-close"})
        first = None
        async for chunk in hook:
            first = chunk
            break
        await hook.aclose()
        return first

    first = asyncio.run(drain())
    assert first is not None and closed == [True]
    tc._STREAM_OBSERVATIONS.clear()


def test_a_stream_without_a_call_id_passes_through_unobserved():
    """No litellm_call_id (a gateway old enough not to send one): every chunk still passes
    through, nothing is recorded, and the success logger finds nothing to take out — no sample
    rather than a wrong one."""
    t0 = datetime(2026, 10, 12, 19, 0, 0)
    chunks = [(t0, _chunk(content="a")), (t0 + timedelta(seconds=1), _chunk(content="b"))]
    passed = _stream_through_hook("", chunks)
    assert passed == [c for _, c in chunks]
    assert "" not in tc._STREAM_OBSERVATIONS
    kw = {**_kwargs(GPU_GGUF, "http://llamacpp:8080/v1"), "completion_start_time": t0}
    assert tc._build_payload(kw, _response(completion_tokens=30), t0, t0 + timedelta(seconds=2)) is None


# --- live work events (/api/work/events) ---

SECRET_KEY = "sk-not-a-real-key-0123456789"


def _call_kwargs(*, alias: str | None = "agent-key", model_group: str = "local-chat",
                 api_base: str = "http://llamacpp:8080/v1", original_group: str | None = None,
                 no_log: bool = False) -> dict:
    """Request kwargs as LiteLLM 1.100.1 hands them to the pre-call and end hooks
    (Logging.model_call_details): the router/proxy metadata under litellm_params."""
    metadata = {"model_group": model_group, "user_api_key_hash": "hash-of-" + SECRET_KEY,
                "api_base": api_base, "model_info": {"weights_file": GPU_GGUF}}
    if alias:
        metadata["user_api_key_alias"] = alias
    if original_group:
        metadata["original_model_group"] = original_group
    params = {"metadata": metadata, "api_base": api_base, "api_key": SECRET_KEY,
              "proxy_server_request": {"headers": {"user-agent": "python-httpx/0.28", "authorization": SECRET_KEY}}}
    if no_log:
        params["no-log"] = True
    return {"model": f"openai/{model_group}", "litellm_call_id": "call-1", "litellm_params": params,
            "api_key": SECRET_KEY}


def test_started_event_names_the_caller_group_and_engine_but_never_the_key():
    import json

    event = tc._started_event(_call_kwargs())
    assert {k: v for k, v in event.items() if k != "started_at"} == {
        "event": "started", "call_id": "call-1", "caller": "agent-key", "model_group": "local-chat",
        "model": "Qwen-GPU-Q6_K.gguf", "deployment": "llamacpp", "fallback": False,
    }
    assert isinstance(event["started_at"], float)
    assert SECRET_KEY not in json.dumps(event)


def test_a_keyless_caller_is_named_by_its_client():
    assert tc._started_event(_call_kwargs(alias=None))["caller"] == "python"


def test_the_fallback_hop_is_flagged_by_the_gates_test():
    hop = tc._started_event(_call_kwargs(model_group="qwen-cpu", api_base="http://llamacpp-cpu:8080/v1",
                                         original_group="local-chat"))
    assert (hop["fallback"], hop["model_group"], hop["deployment"]) == (True, "qwen-cpu", "llamacpp-cpu")
    direct = tc._started_event(_call_kwargs(model_group="qwen-cpu", api_base="http://llamacpp-cpu:8080/v1"))
    assert direct["fallback"] is False


def test_health_probes_send_no_events():
    """LiteLLM skips the end hooks for `no-log` probes, so a start would never end."""
    assert tc._started_event(_call_kwargs(no_log=True)) is None
    assert tc._ended_event(_call_kwargs(no_log=True), _response(), datetime.now(), datetime.now(), False) is None


def test_ended_event_carries_usage_timing_and_outcome():
    start = datetime(2026, 10, 17, 12, 0, 0)
    event = tc._ended_event(_call_kwargs(), _response(completion_tokens=64), start, start + timedelta(seconds=3),
                            failed=False)
    assert event["event"] == "ended"
    assert (event["prompt_tokens"], event["completion_tokens"], event["outcome"]) == (10, 64, "ok")
    assert event["ended_at"] - event["started_at"] == 3.0
    assert "error" not in event


def test_a_failed_attempt_ends_with_the_error_class():
    class InternalServerError(Exception):
        pass

    kwargs = {**_call_kwargs(), "exception": InternalServerError("engine down")}
    start = datetime(2026, 10, 17, 12, 0, 0)
    event = tc._ended_event(kwargs, None, start, start + timedelta(seconds=1), failed=True)
    assert (event["outcome"], event["error"], event["completion_tokens"]) == ("error", "InternalServerError", None)


def test_prompt_tokens_count_only_the_text_parts(monkeypatch):
    counted = []
    monkeypatch.setattr(tc.litellm, "token_counter", lambda text: counted.append(text) or 42, raising=False)
    messages = [{"role": "system", "content": "be brief"},
                {"role": "user", "content": [{"type": "text", "text": "describe"},
                                             {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]
    assert tc._count_prompt_tokens(messages) == 42
    assert counted == ["be brief\ndescribe"]
    assert tc._count_prompt_tokens([]) is None


class _FailingClient:
    """An httpx.AsyncClient whose every POST fails, recording the URL it was asked for."""

    posted: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        _FailingClient.posted.append(url)
        raise tc.httpx.ConnectError("dashboard down")


def test_a_failing_dashboard_never_raises_into_the_request(monkeypatch):
    import asyncio

    monkeypatch.setattr(tc.httpx, "AsyncClient", _FailingClient)
    _FailingClient.posted = []
    recorder = tc.ThroughputRecorder()
    start = datetime(2026, 10, 17, 12, 0, 0)
    end = start + timedelta(seconds=2)

    async def one_request():
        recorder.log_pre_api_call("openai/local-chat", [{"role": "user", "content": "hi"}], _call_kwargs())
        await asyncio.gather(*tc._background_tasks)
        await recorder.async_log_success_event(_call_kwargs(), _response(predicted_per_second=30.0), start, end)
        await recorder.async_log_failure_event({**_call_kwargs(), "exception": ValueError("x")}, None, start, end)

    asyncio.run(one_request())
    assert [url.rsplit("8080", 1)[-1] for url in _FailingClient.posted] == [
        "/api/work/events", "/api/work/events", "/api/throughput/record", "/api/work/events"]


def test_a_broken_event_never_raises_into_the_request():
    recorder = tc.ThroughputRecorder()
    recorder.log_pre_api_call("m", [], {"litellm_call_id": "c", "litellm_params": "not-a-dict"})
