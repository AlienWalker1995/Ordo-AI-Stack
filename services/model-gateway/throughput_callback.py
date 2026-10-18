"""LiteLLM custom callback — posts per-completion throughput samples to the
Ordo dashboard at /api/throughput/record.

Wired in `litellm_config.yaml` as:
    litellm_settings:
      callbacks: ["throughput_callback.throughput_recorder_instance"]

The dashboard's `/api/throughput/record` endpoint accepts:
    {model, output_tokens_per_sec, ttft_ms, service, alias?, backend?}

This callback fires after every successful completion and POSTs a fire-and-
forget sample. Never raises into the inference path — telemetry failures are
logged at WARN and swallowed so a dashboard outage cannot break /v1/chat.

It also reports live work to the dashboard's `/api/work/events` (same token): a `started`
event when LiteLLM hands a request to an engine (the sync `log_pre_api_call` hook; LiteLLM
1.100.1 never calls `async_log_pre_api_call`) and an `ended` event from the success and
failure hooks (a stream ends at its final success event). Each attempt is one pair, so a
GPU error followed by the CPU fallback hop is two pairs under one call_id.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import litellm
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("throughput_recorder")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080").rstrip("/")
RECORD_PATH = "/api/throughput/record"
WORK_EVENTS_PATH = "/api/work/events"
TOKEN = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("THROUGHPUT_POST_TIMEOUT_SEC", "1.5"))


def _load_fallback_gate():
    """The sibling gpu_lease_fallback_gate module, for its fallback-hop test.

    LiteLLM loads callbacks by file path without adding their directory to sys.path, so a
    plain `import gpu_lease_fallback_gate` fails in the container; the entrypoint copies both
    files next to the rendered config, so load it from beside this one."""
    path = Path(__file__).with_name("gpu_lease_fallback_gate.py")
    spec = importlib.util.spec_from_file_location("gpu_lease_fallback_gate_for_throughput", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_fallback_gate = _load_fallback_gate()

# User-Agent substrings → friendly service name. First match wins; ordered most-
# specific first. Unknown UAs fall through to "unknown".
_UA_MAP: tuple[tuple[str, str], ...] = (
    ("open-webui", "open-webui"),
    ("openwebui", "open-webui"),
    ("hermes", "hermes"),
    ("cline", "cline"),
    ("continue", "continue"),
    ("claude-code", "claude-code"),
    ("python-httpx", "python"),
    ("python-requests", "python"),
    ("node-fetch", "node"),
    ("undici", "node"),
    ("curl", "curl"),
)


def _detect_service(kwargs: dict[str, Any]) -> str:
    """Identify the caller from request metadata.

    Source of truth is the StandardLoggingPayload that LiteLLM places in
    kwargs["standard_logging_object"] for every completion. It carries the
    User-Agent and any `x-*` custom headers the client sent.

    Preference order:
      1. Explicit `X-Hermes-Service` custom header
      2. User-Agent prefix matched against _UA_MAP
      3. First slash-delimited token of User-Agent (e.g. "MyApp/1.0" → "MyApp")
      4. proxy_server_request headers fallback (older LiteLLM versions)
      5. "unknown"
    """
    slo = kwargs.get("standard_logging_object") or {}
    custom = slo.get("requester_custom_headers") or {}
    # Custom-header keys come through lower-cased by Starlette/Uvicorn, but
    # normalize defensively.
    if hasattr(custom, "items"):
        custom_norm = {str(k).lower(): str(v) for k, v in custom.items()}
    else:
        custom_norm = {}
    explicit = custom_norm.get("x-hermes-service")
    if explicit:
        return explicit[:64]
    ua = (slo.get("user_agent") or "").strip()
    if not ua:
        # Older LiteLLM paths still expose proxy_server_request — try that too.
        params = kwargs.get("litellm_params") or {}
        req = kwargs.get("proxy_server_request") or params.get("proxy_server_request") or {}
        headers = req.get("headers") or {}
        if hasattr(headers, "items"):
            norm = {str(k).lower(): str(v) for k, v in headers.items()}
            ua = (norm.get("user-agent") or "").strip()
    if ua:
        lo = ua.lower()
        for needle, name in _UA_MAP:
            if needle in lo:
                return name
        return ua.split("/")[0][:64]
    return "unknown"


def _deployment_model_info(kwargs: dict[str, Any]) -> dict:
    """The served deployment's model_info (including our custom `weights_file`).

    LiteLLM v1.82.3's Router._update_kwargs_with_deployment attaches it to the
    request kwargs — top-level, and inside the router metadata (which the logger
    later moves under litellm_params). Check every location; first hit wins."""
    for candidate in (
        kwargs.get("model_info"),
        ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("model_info"),
        (kwargs.get("metadata") or {}).get("model_info"),
    ):
        if isinstance(candidate, dict) and candidate.get("weights_file"):
            return candidate
    return {}


def _served_api_base(kwargs: dict[str, Any]) -> str:
    slo = kwargs.get("standard_logging_object") or {}
    return str(
        slo.get("api_base")
        or ((kwargs.get("litellm_params") or {}).get("metadata") or {}).get("api_base")
        or (kwargs.get("metadata") or {}).get("api_base")
        # Before the call (log_pre_api_call) only LiteLLM's own record of the target is set.
        or (kwargs.get("litellm_params") or {}).get("api_base")
        or ""
    )


def _detect_backend(kwargs: dict[str, Any]) -> str:
    """Backend service that served the request, from the deployment api_base host
    (e.g. 'llamacpp' vs 'llamacpp-cpu' — the failover distinction). '' if unknown."""
    try:
        return (urlparse(_served_api_base(kwargs)).hostname or "")[:64]
    except ValueError:
        return ""


def _detect_alias(kwargs: dict[str, Any]) -> str:
    """The model id the CALLER requested (model_group), e.g. 'local-chat'."""
    slo = kwargs.get("standard_logging_object") or {}
    group = str(slo.get("model_group") or "").strip()
    if group:
        return group[:256]
    kwarg_model = str(kwargs.get("model") or "").strip()
    if "/" in kwarg_model:
        kwarg_model = kwarg_model.split("/", 1)[1]
    return kwarg_model[:256]


def _resolve_model(kwargs: dict[str, Any], response_obj: Any) -> str:
    """Identify the model that ACTUALLY served the completion.

    Truth source: the served deployment's model_info.weights_file — the exact GGUF,
    entrypoint-substituted from the same .env the llama-server reads — so samples
    key by real model, never by a routing alias like `local-chat` (which conflates
    every model ever active behind it, CPU failover included). Falls back to the
    legacy response/kwargs naming when the router info is missing; telemetry is
    never dropped over attribution."""
    weights = str(_deployment_model_info(kwargs).get("weights_file") or "").strip()
    if weights:
        return weights.replace("\\", "/").rsplit("/", 1)[-1].split(":")[0][:256]
    resp_model = ""
    if isinstance(response_obj, dict):
        resp_model = str(response_obj.get("model") or "").strip()
    elif hasattr(response_obj, "model"):
        resp_model = str(getattr(response_obj, "model", "") or "").strip()
    kwarg_model = str(kwargs.get("model") or "").strip()
    # LiteLLM kwargs sometimes prefix the provider, e.g. "openai/local-chat".
    if "/" in kwarg_model:
        kwarg_model = kwarg_model.split("/", 1)[1]
    chosen = resp_model or kwarg_model
    return chosen.split(":")[0][:256]


def _ttft_ms_from_kwargs(kwargs: dict[str, Any], start_time: Any) -> float:
    """Compute time-to-first-token in milliseconds if LiteLLM exposed it."""
    start_stream = kwargs.get("completion_start_time")
    if not start_stream or not start_time:
        return 0.0
    try:
        if isinstance(start_stream, datetime) and isinstance(start_time, datetime):
            return max(0.0, (start_stream - start_time).total_seconds() * 1000.0)
    except Exception:
        pass
    return 0.0


def _duration_sec(start_time: Any, end_time: Any) -> float:
    if isinstance(start_time, datetime) and isinstance(end_time, datetime):
        return max(0.0, (end_time - start_time).total_seconds())
    try:
        return max(0.0, float(end_time) - float(start_time))
    except Exception:
        return 0.0


def _engine_timings(response_obj: Any) -> dict[str, Any]:
    """The llama.cpp-style `timings` block the engine returned (llama.cpp and NInfer both send it)."""
    if isinstance(response_obj, dict):
        timings = response_obj.get("timings")
    else:
        timings = getattr(response_obj, "timings", None)
    return timings if isinstance(timings, dict) else {}


# Streamed replies need their own clock. The gateway assembles the chunks into one response and
# drops the engine's `timings` (sent on the final chunk), and LiteLLM's completion_start_time is
# the first chunk of any kind: NInfer sends an empty role chunk the moment a request is queued, so
# "first chunk to end" still counted minutes of waiting. The streaming iterator hook records, per
# litellm_call_id, the final chunk's timings and when the first token-bearing chunk arrived; the
# success logger takes them out. Bounded, so streams that never log success cannot pile up.
_STREAM_OBSERVATIONS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_MAX_STREAM_OBSERVATIONS = 1024


def _chunk_carries_tokens(chunk: Any) -> bool:
    """True for a chunk with generated output: content, reasoning or a tool call."""
    for choice in getattr(chunk, "choices", None) or []:
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if (getattr(delta, "content", None) or getattr(delta, "reasoning_content", None)
                or getattr(delta, "tool_calls", None)):
            return True
    return False


def _observe_stream_chunk(call_id: str, chunk: Any, now: datetime) -> None:
    observation = _STREAM_OBSERVATIONS.get(call_id)
    if observation is None:
        observation = _STREAM_OBSERVATIONS[call_id] = {}
        while len(_STREAM_OBSERVATIONS) > _MAX_STREAM_OBSERVATIONS:
            _STREAM_OBSERVATIONS.popitem(last=False)
    if "first_token" not in observation and _chunk_carries_tokens(chunk):
        observation["first_token"] = now
    timings = _engine_timings(chunk)
    if timings:
        observation["timings"] = timings


def _predicted_per_second(timings: dict[str, Any]) -> float | None:
    try:
        predicted = float(timings.get("predicted_per_second") or 0)
    except (TypeError, ValueError):
        return None
    return predicted if predicted > 0 else None


def _generation_tokens_per_sec(response_obj, kwargs, completion_tokens: int, end_time: Any) -> float | None:
    """How fast the engine generated, excluding time spent queued and reading the prompt.

    Dividing by the whole request time instead reported a 206 tok/s engine as 17 tok/s while
    requests waited minutes in its queue. In order: the engine's own `predicted_per_second` on the
    response, then on the stream's final chunk, then tokens over first-token-bearing-chunk to end.
    None of those: no sample, rather than one that mostly measures waiting.
    """
    # Take the observation out before any early return: whichever timing source wins, the
    # stream's entry leaves the bounded map exactly once.
    observation = _STREAM_OBSERVATIONS.pop(str(kwargs.get("litellm_call_id") or ""), None) or {}
    predicted = _predicted_per_second(_engine_timings(response_obj))
    if predicted:
        return predicted
    predicted = _predicted_per_second(observation.get("timings") or {})
    if predicted:
        return predicted
    first_token = observation.get("first_token")
    if isinstance(first_token, datetime) and isinstance(end_time, datetime):
        generating = (end_time - first_token).total_seconds()
        if generating > 0:
            return completion_tokens / generating
    return None


def _usage(response_obj: Any) -> dict[str, Any]:
    """The response's token usage as a dict ({} when the response carries none)."""
    if isinstance(response_obj, dict):
        return response_obj.get("usage") or {}
    u = getattr(response_obj, "usage", None)
    if isinstance(u, dict):
        return u
    if u is not None:
        return {
            "completion_tokens": getattr(u, "completion_tokens", 0),
            "prompt_tokens": getattr(u, "prompt_tokens", 0),
        }
    return {}


def _build_payload(kwargs, response_obj, start_time, end_time) -> dict[str, Any] | None:
    duration = _duration_sec(start_time, end_time)
    if duration <= 0:
        return None
    usage = _usage(response_obj)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if completion_tokens <= 0:
        return None
    model = _resolve_model(kwargs, response_obj)
    if not model:
        return None
    tokens_per_sec = _generation_tokens_per_sec(response_obj, kwargs, completion_tokens, end_time)
    if tokens_per_sec is None:
        return None
    payload = {
        "model": model,
        "output_tokens_per_sec": round(tokens_per_sec, 2),
        "service": _detect_service(kwargs),
        "ttft_ms": round(_ttft_ms_from_kwargs(kwargs, start_time), 1),
    }
    alias = _detect_alias(kwargs)
    backend = _detect_backend(kwargs)
    if alias:
        payload["alias"] = alias
    if backend:
        payload["backend"] = backend
    return payload


# --- live work events (/api/work/events) ---

def _request_metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The proxy and router metadata of the request. Chat completions carry it under
    litellm_params.metadata, /v1/messages under litellm_params.litellm_metadata; later
    sources win when a key is in both."""
    params = kwargs.get("litellm_params") or {}
    merged: dict[str, Any] = {}
    for candidate in (kwargs.get("litellm_metadata"), kwargs.get("metadata"),
                      params.get("litellm_metadata"), params.get("metadata")):
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def _detect_caller(kwargs: dict[str, Any]) -> str:
    """Who is calling: the virtual key's alias (never the key or its hash), else the client the
    request headers name (_detect_service)."""
    alias = str(_request_metadata(kwargs).get("user_api_key_alias") or "").strip()
    if alias:
        return alias[:64]
    return _detect_service(kwargs)


def _model_group(kwargs: dict[str, Any]) -> str:
    """The model group this attempt was routed to: the requested alias, or the fallback's group."""
    group = str(_request_metadata(kwargs).get("model_group") or "").strip()
    return group[:256] if group else _detect_alias(kwargs)


def _is_fallback(kwargs: dict[str, Any]) -> bool:
    """Whether this attempt is local-chat's fallback hop, by the gate's own test."""
    return _fallback_gate._is_fallback_hop(_model_group(kwargs), {"metadata": _request_metadata(kwargs)})


def _prompt_text(messages: Any) -> str:
    """The text of the prompt's messages. Images and other parts are left out: counting an
    image_url makes LiteLLM's counter fetch the image."""
    if not isinstance(messages, list):
        return ""
    texts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [str(part["text"]) for part in content if isinstance(part, dict) and part.get("text")]
    return "\n".join(texts)


def _count_prompt_tokens(messages: Any) -> int | None:
    """An estimate of the prompt's size with LiteLLM's default tokenizer (tiktoken, about 20 ms
    for 170k tokens, so it runs off the event loop). The engine's own count arrives with the
    ended event. None when it cannot be counted."""
    text = _prompt_text(messages)
    if not text:
        return None
    try:
        return int(litellm.token_counter(text=text))
    except Exception as exc:
        logger.debug("throughput_recorder prompt token count failed: %s", exc)
        return None


def _epoch(value: Any) -> float | None:
    """Epoch seconds from LiteLLM's start/end time (a naive local datetime, or a number)."""
    if isinstance(value, datetime):
        return round(value.timestamp(), 3)
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _event_base(kwargs: dict[str, Any], event: str) -> dict[str, Any] | None:
    """Fields both events carry, or None when the request has no call id or is a health probe
    (`no-log`: LiteLLM skips the end hooks for those, so a start would never end)."""
    call_id = str(kwargs.get("litellm_call_id") or "").strip()
    if not call_id or (kwargs.get("litellm_params") or {}).get("no-log") is True:
        return None
    return {
        "event": event,
        "call_id": call_id[:128],
        "caller": _detect_caller(kwargs),
        "model_group": _model_group(kwargs),
        "model": _resolve_model(kwargs, None),
        "deployment": _detect_backend(kwargs),
        "fallback": _is_fallback(kwargs),
    }


def _started_event(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    event = _event_base(kwargs, "started")
    if event is not None:
        event["started_at"] = round(time.time(), 3)
    return event


def _ended_event(kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any,
                 failed: bool) -> dict[str, Any] | None:
    event = _event_base(kwargs, "ended")
    if event is None:
        return None
    usage = {} if failed else _usage(response_obj)
    event.update({
        "model": _resolve_model(kwargs, None if failed else response_obj),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0) or None,
        "completion_tokens": int(usage.get("completion_tokens") or 0) or None,
        "outcome": "error" if failed else "ok",
        "started_at": _epoch(start_time),
        "ended_at": _epoch(end_time) or round(time.time(), 3),
    })
    if failed:
        exception = kwargs.get("exception")
        if exception is None and isinstance(response_obj, BaseException):
            exception = response_obj
        event["error"] = type(exception).__name__ if exception is not None else "unknown"
    return event


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["X-Throughput-Token"] = TOKEN
    return h


class ThroughputRecorder(CustomLogger):
    """LiteLLM CustomLogger — posts samples to the dashboard.

    Both sync and async hooks are implemented so the recorder works whether
    LiteLLM dispatches via the sync or async code path.
    """

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        """Pass every chunk through unchanged, noting when generation really started."""
        call_id = str((request_data or {}).get("litellm_call_id") or "")
        async for chunk in response:
            if call_id:
                try:
                    _observe_stream_chunk(call_id, chunk, datetime.now())
                except Exception as exc:  # telemetry must never break a stream
                    logger.debug("throughput_recorder could not observe a chunk: %s", exc)
            yield chunk

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        await self._post_ended(kwargs, response_obj, start_time, end_time, failed=False)
        payload = _build_payload(kwargs, response_obj, start_time, end_time)
        if not payload:
            return
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                await client.post(
                    f"{DASHBOARD_URL}{RECORD_PATH}", json=payload, headers=_headers()
                )
        except Exception as exc:
            logger.warning("throughput_recorder async post failed: %s", exc)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._fire(self._post_ended(kwargs, response_obj, start_time, end_time, failed=False))
        payload = _build_payload(kwargs, response_obj, start_time, end_time)
        if not payload:
            return
        # Fire from a background task so we never block the request thread.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._post_async(payload))
                return
        except RuntimeError:
            pass
        try:
            httpx.post(
                f"{DASHBOARD_URL}{RECORD_PATH}",
                json=payload,
                headers=_headers(),
                timeout=TIMEOUT,
            )
        except Exception as exc:
            logger.warning("throughput_recorder sync post failed: %s", exc)

    async def _post_async(self, payload: dict[str, Any], path: str = RECORD_PATH) -> None:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                await client.post(
                    f"{DASHBOARD_URL}{path}", json=payload, headers=_headers()
                )
        except Exception as exc:
            logger.warning("throughput_recorder backgrounded post to %s failed: %s", path, exc)

    # --- live work events ---

    def log_pre_api_call(self, model, messages, kwargs):
        """LiteLLM calls this synchronously on the request's event loop just before the engine
        call, so the event (and its token count) goes out on a background task."""
        try:
            event = _started_event(kwargs)
        except Exception as exc:
            logger.warning("throughput_recorder started event failed: %s", exc)
            return
        if event is not None:
            self._fire(self._post_started(event, messages))

    async def _post_started(self, event: dict[str, Any], messages: Any) -> None:
        try:
            event["prompt_tokens"] = await asyncio.to_thread(_count_prompt_tokens, messages)
        except Exception as exc:
            logger.warning("throughput_recorder prompt token count failed: %s", exc)
            event["prompt_tokens"] = None
        await self._post_async(event, WORK_EVENTS_PATH)

    async def _post_ended(self, kwargs, response_obj, start_time, end_time, failed: bool) -> None:
        try:
            event = _ended_event(kwargs, response_obj, start_time, end_time, failed)
        except Exception as exc:
            logger.warning("throughput_recorder ended event failed: %s", exc)
            return
        if event is not None:
            await self._post_async(event, WORK_EVENTS_PATH)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        await self._post_ended(kwargs, response_obj, start_time, end_time, failed=True)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._fire(self._post_ended(kwargs, response_obj, start_time, end_time, failed=True))

    def _fire(self, coroutine) -> None:
        """Run `coroutine` without waiting for it: on the running loop when there is one (the
        proxy's case), else on a daemon thread, so a slow dashboard never holds a request."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            threading.Thread(target=asyncio.run, args=(coroutine,), daemon=True).start()
            return
        task = loop.create_task(coroutine)
        # The loop keeps only a weak reference to a task; hold it until it finishes.
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


_background_tasks: set[asyncio.Task] = set()


# Module-level singleton — referenced from litellm_config.yaml.
throughput_recorder_instance = ThroughputRecorder()
