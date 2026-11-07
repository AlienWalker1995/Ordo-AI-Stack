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
to the CPU: the GPU is healthy, and a CPU run beside it reads a ~100k-token prompt at about
60-140 tok/s (15-30 minutes) before it starts answering. The hop is refused, so the caller gets the GPU's own queue
error (429 queue full, 503 expired; the router re-raises the original error when every fallback
fails) and retries. The refusal names no deployment, so it never puts the CPU model in cooldown.

This hook runs on the fallback hop and logs the lease state for observability.

The lease state is read from ops-controller's unauthenticated GET /lease, a JSON answer served
from memory in milliseconds: {"evicted_residents": [...], "gpu_chat_up": true|false|null}.
"inference-engine-gpu" in evicted_residents means a render has evicted the GPU chat model, and
gpu_chat_up false means the chat engine is down. (It does not read /metrics: that scrape runs docker
and probes the engine, and took longer than the gate's timeout exactly during lease transitions.)
The parsed state is cached for 5 s; an unreadable or unparseable response is logged (at most
once per minute) and treated as "allowed" (the fallback is allowed regardless).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
import litellm
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("gpu_lease_fallback_gate")

# ops-controller serves the GPU lease/eviction state unauthenticated, the same as /health.
LEASE_URL = os.environ.get("GPU_LEASE_GATE_URL", "http://ops-controller:9000/lease")
LEASE_TIMEOUT = float(os.environ.get("GPU_LEASE_GATE_TIMEOUT_SEC", "2"))
CACHE_TTL = float(os.environ.get("GPU_LEASE_GATE_CACHE_TTL_SEC", "5"))
# An unreachable ops-controller is logged once per this many seconds, not once per scrape.
UNREADABLE_WARN_INTERVAL = 60.0

# The messages NInfer puts on its queue errors (pinned engine commit 77c2e5e2,
# src/serve/generation_service.cpp and src/runtime/engine/engine_core.h): 429 "inference request
# queue is full", and 503 "inference request expired before submission / while waiting for
# admission / during preparation". LiteLLM keeps the message in the failed attempt's breadcrumb.
GPU_QUEUE_ERROR_MARKERS = ("inference request queue is full", "inference request expired")

GPU_CHAT_RESIDENT = "inference-engine-gpu"


def fallback_allowed(lease: Any) -> bool:
    """Whether the CPU fallback may serve, from one ops-controller GET /lease answer (decoded JSON).

    Allowed while the GPU chat model is evicted for a lease (it is in evicted_residents) or the GPU
    chat service is down (gpu_chat_up is false). When neither fact is present there is nothing to
    judge by, so the gate fails open: a consumer must not be locked out because ops-controller
    stopped reporting a field. Only a known-healthy, resident GPU is refused.
    """
    if not isinstance(lease, dict):
        return True
    evicted_residents = lease.get("evicted_residents")
    evicted = GPU_CHAT_RESIDENT in evicted_residents if isinstance(evicted_residents, list) else None
    if evicted:
        return True
    chat_up = lease.get("gpu_chat_up")
    chat_up = chat_up if isinstance(chat_up, bool) else None
    if chat_up is False:
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
        # (expiry as time.monotonic(), parsed allowed state) of the last lease read.
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

        Fails open: when the lease state cannot be read or parsed, the fallback is allowed
        (availability first) and the warning is logged at most once per
        UNREADABLE_WARN_INTERVAL.
        """
        now = time.monotonic()
        cached = self._cache
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=LEASE_TIMEOUT) as client:
                response = await client.get(LEASE_URL)
                response.raise_for_status()
                allowed = fallback_allowed(response.json())
        except Exception as exc:
            last = self._last_unreadable_warning
            if last is None or now - last >= UNREADABLE_WARN_INTERVAL:
                logger.warning(
                    "gpu_lease_fallback_gate: cannot read %s (%s); allowing the CPU fallback",
                    LEASE_URL,
                    exc,
                )
                self._last_unreadable_warning = now
            allowed = True
        self._cache = (now + CACHE_TTL, allowed)
        return allowed


# Module-level singleton, referenced from litellm_config.yaml (litellm_settings.callbacks).
gpu_lease_fallback_gate_instance = GpuLeaseFallbackGate()
