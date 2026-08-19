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
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("throughput_recorder")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://dashboard:8080").rstrip("/")
RECORD_PATH = "/api/throughput/record"
TOKEN = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
TIMEOUT = float(os.environ.get("THROUGHPUT_POST_TIMEOUT_SEC", "1.5"))

# User-Agent substrings → friendly service name. First match wins; ordered most-
# specific first. Unknown UAs fall through to "unknown".
_UA_MAP: tuple[tuple[str, str], ...] = (
    ("open-webui", "open-webui"),
    ("openwebui", "open-webui"),
    ("hermes", "hermes"),
    ("n8n", "n8n"),
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
        req = kwargs.get("proxy_server_request") or {}
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


def _build_payload(kwargs, response_obj, start_time, end_time) -> dict[str, Any] | None:
    duration = _duration_sec(start_time, end_time)
    if duration <= 0:
        return None
    usage: dict[str, Any] = {}
    if isinstance(response_obj, dict):
        usage = response_obj.get("usage") or {}
    elif hasattr(response_obj, "usage"):
        u = getattr(response_obj, "usage", None)
        if isinstance(u, dict):
            usage = u
        elif u is not None:
            usage = {
                "completion_tokens": getattr(u, "completion_tokens", 0),
                "prompt_tokens": getattr(u, "prompt_tokens", 0),
            }
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if completion_tokens <= 0:
        return None
    model = _resolve_model(kwargs, response_obj)
    if not model:
        return None
    payload = {
        "model": model,
        "output_tokens_per_sec": round(completion_tokens / duration, 2),
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

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
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

    async def _post_async(self, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                await client.post(
                    f"{DASHBOARD_URL}{RECORD_PATH}", json=payload, headers=_headers()
                )
        except Exception as exc:
            logger.warning("throughput_recorder backgrounded post failed: %s", exc)


# Module-level singleton — referenced from litellm_config.yaml.
throughput_recorder_instance = ThroughputRecorder()
