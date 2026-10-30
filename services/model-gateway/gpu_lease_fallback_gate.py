"""LiteLLM custom callback: local-chat's CPU fallback, allowed for every GPU failure but a busy queue.

Wired in `litellm_config.yaml` as:
    litellm_settings:
      callbacks: ["gpu_lease_fallback_gate.gpu_lease_fallback_gate_instance"]

router_settings.fallbacks sends ANY error from the GPU deployment (busy queue, 400, 500,
mid-stream) to the CPU model. Operator policy (2026-10-06): a GPU failure must never
kill a local-chat consumer (Claude Code, Hermes, ...), so the CPU fallback is allowed
whether the GPU is evicted for a render, down, or returned an error.

One exception (operator policy 2026-10-30): chat runs one request at a time, first come
first served, on the GPU. A request the GPU refused only because its queue was full or the
request waited too long (NInfer's own queue errors, GPU_QUEUE_ERROR_MARKERS) is not moved
to the CPU: the GPU is healthy, and a CPU run beside it reads a ~100k-token prompt at a few
tokens a second for over an hour. The hop is refused, so the caller gets the GPU's own queue
error (429 queue full, 503 expired; the router re-raises the original error when every fallback
fails) and retries. The refusal names no deployment, so it never puts the CPU model in cooldown.

This hook runs on the fallback hop and logs the lease state (read from ops-controller's
unauthenticated /metrics) for observability.

The lease state is read from ops-controller's unauthenticated /metrics (Prometheus
text): ordo_gpu_resident_evicted{resident="inference-engine-gpu"} is 1 while a render has evicted
the GPU chat model, and ordo_gpu_chat_up is 0 while the chat engine is down. The parsed
state is cached for 5 s; an unreadable or unparseable metrics response is logged (at most
once per minute) and treated as "not evicted" (the fallback is allowed regardless).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx
import litellm
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("gpu_lease_fallback_gate")

# ops-controller serves the GPU lease/eviction state unauthenticated (Prometheus scrapes
# it without a token), the same as /health.
METRICS_URL = os.environ.get("GPU_LEASE_GATE_METRICS_URL", "http://ops-controller:9000/metrics")
METRICS_TIMEOUT = float(os.environ.get("GPU_LEASE_GATE_TIMEOUT_SEC", "2"))
CACHE_TTL = float(os.environ.get("GPU_LEASE_GATE_CACHE_TTL_SEC", "5"))
# An unreachable ops-controller is logged once per this many seconds, not once per scrape.
UNREADABLE_WARN_INTERVAL = 60.0

# The messages NInfer puts on its queue errors (pinned engine commit d44ab584,
# src/serve/generation_service.cpp and src/runtime/engine/engine_core.h): 429 "inference request
# queue is full", and 503 "inference request expired before submission / while waiting for
# admission / during preparation". LiteLLM keeps the message in the failed attempt's breadcrumb.
GPU_QUEUE_ERROR_MARKERS = ("inference request queue is full", "inference request expired")

# The two series ops-controller exports for the GPU chat service (ordo/control/metrics.py):
# one per resident (1 while stopped for a lease) and the /health liveness of the chat engine.
_EVICTED_RE = re.compile(r'^ordo_gpu_resident_evicted\{resident="inference-engine-gpu"\}\s+(\S+)', re.MULTILINE)
_CHAT_UP_RE = re.compile(r"^ordo_gpu_chat_up\s+(\S+)", re.MULTILINE)


def _parse_value(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def _first_value(pattern: re.Pattern[str], text: str) -> float | None:
    """The value of the first sample matching `pattern`, or None when absent or malformed."""
    match = pattern.search(text)
    if match is None:
        return None
    return _parse_value(match.group(1))


def fallback_allowed(metrics_text: str) -> bool:
    """Whether the CPU fallback may serve, from one ops-controller /metrics scrape.

    Allowed while the GPU chat model is evicted for a lease
    (ordo_gpu_resident_evicted{resident="inference-engine-gpu"} 1) or the GPU chat service is down
    (ordo_gpu_chat_up 0). When neither series is present there is nothing to judge by,
    so the gate fails open: a consumer must not be locked out because ops-controller
    stopped exporting a line.
    """
    evicted = _first_value(_EVICTED_RE, metrics_text)
    if evicted == 1.0:
        return True
    chat_up = _first_value(_CHAT_UP_RE, metrics_text)
    if chat_up == 0.0:
        return True
    if evicted is None and chat_up is None:
        return True
    return False


def _is_fallback_hop(model: str, request_kwargs: dict[str, Any] | None) -> bool:
    """True only on a fallback hop out of local-chat.

    The router records the group the caller asked for as original_model_group in the
    request metadata; a hop is that marker present while the deployment being filtered
    is not local-chat itself. Direct calls (local-chat, or the CPU model by name) pass
    through untouched.
    """
    if not model or model == "local-chat":
        return False
    if not isinstance(request_kwargs, dict):
        return False
    for key in ("metadata", "litellm_metadata"):
        meta = request_kwargs.get(key)
        if isinstance(meta, dict) and meta.get("original_model_group") == "local-chat":
            return True
    return False


def _last_gpu_error(request_kwargs: dict[str, Any] | None) -> str:
    """The error text of the attempt this hop falls back from, or "" when the router left none.

    Router.log_retry appends one breadcrumb per failed attempt to the request metadata's
    previous_models, each carrying exception_string; the last one is the GPU failure.
    """
    if not isinstance(request_kwargs, dict):
        return ""
    for key in ("metadata", "litellm_metadata"):
        meta = request_kwargs.get(key)
        if not isinstance(meta, dict):
            continue
        breadcrumbs = meta.get("previous_models")
        if isinstance(breadcrumbs, list | tuple) and breadcrumbs:
            last = breadcrumbs[-1]
            if hasattr(last, "get"):
                return str(last.get("exception_string") or "")
    return ""


def is_gpu_queue_error(error_text: str) -> bool:
    """True when the GPU refused the request only because its queue was full or it waited too long."""
    return any(marker in error_text for marker in GPU_QUEUE_ERROR_MARKERS)


class GpuLeaseFallbackGate(CustomLogger):
    """LiteLLM CustomLogger: allows local-chat's CPU fallback except for a busy GPU queue, and
    logs the GPU lease state for observability (see the module docstring)."""

    def __init__(self) -> None:
        # (expiry as time.monotonic(), parsed allowed state) of the last metrics read.
        self._cache: tuple[float, bool] | None = None
        # time.monotonic() of the last "cannot read" warning, or None before the first.
        self._last_unreadable_warning: float | None = None

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list[Any],
        messages: list[Any],
        request_kwargs: dict[str, Any] | None = None,
        parent_otel_span: Any = None,
    ) -> list[Any]:
        if not _is_fallback_hop(model, request_kwargs):
            return healthy_deployments
        gpu_error = _last_gpu_error(request_kwargs)
        if is_gpu_queue_error(gpu_error):
            # A busy GPU is not a failed GPU: the request keeps to the one-at-a-time GPU queue.
            logger.info("gpu_lease_fallback_gate: GPU queue busy, not moving the request to %s: %s", model, gpu_error[:200])
            raise litellm.ServiceUnavailableError(
                message=(
                    "The GPU chat queue is busy (one request at a time, first come first served); "
                    "the request was not moved to the CPU fallback. Retry shortly."
                ),
                llm_provider="openai",
                model=model,
            )
        # Every other GPU failure (evicted for a render, down, a bad request, a transient
        # 5xx, a mid-stream drop) degrades to the CPU model instead of failing the caller
        # (operator policy 2026-10-06). The lease state is logged for observability only.
        allowed = await self._fallback_allowed()
        logger.info(
            "gpu_lease_fallback_gate: allowing CPU fallback for %s (lease-allowed=%s)",
            model,
            allowed,
        )
        return healthy_deployments

    async def _fallback_allowed(self) -> bool:
        """The lease state from ops-controller, cached for CACHE_TTL seconds.

        Fails open: when the metrics cannot be read or parsed, the fallback is allowed
        (availability first) and the warning is logged at most once per
        UNREADABLE_WARN_INTERVAL.
        """
        now = time.monotonic()
        cached = self._cache
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=METRICS_TIMEOUT) as client:
                response = await client.get(METRICS_URL)
                response.raise_for_status()
                allowed = fallback_allowed(response.text)
        except Exception as exc:
            last = self._last_unreadable_warning
            if last is None or now - last >= UNREADABLE_WARN_INTERVAL:
                logger.warning(
                    "gpu_lease_fallback_gate: cannot read %s (%s); allowing the CPU fallback",
                    METRICS_URL,
                    exc,
                )
                self._last_unreadable_warning = now
            allowed = True
        self._cache = (now + CACHE_TTL, allowed)
        return allowed


# Module-level singleton, referenced from litellm_config.yaml (litellm_settings.callbacks).
gpu_lease_fallback_gate_instance = GpuLeaseFallbackGate()
