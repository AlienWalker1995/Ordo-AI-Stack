"""LiteLLM custom callback: local-chat's CPU fallback is always allowed.

Wired in `litellm_config.yaml` as:
    litellm_settings:
      callbacks: ["gpu_lease_fallback_gate.gpu_lease_fallback_gate_instance"]

router_settings.fallbacks sends ANY error from the GPU deployment (busy queue, 400, 500,
mid-stream) to the CPU model. Operator policy (2026-10-06): a GPU failure must never
kill a local-chat consumer (Claude Code, Hermes, ...), so the CPU fallback is ALWAYS
allowed — whether the GPU is evicted for a render, down, or merely returned an error.
This hook runs on the fallback hop and logs the lease state (read from ops-controller's
unauthenticated /metrics) for observability, but no longer denies the fallback.

The lease state is read from ops-controller's unauthenticated /metrics (Prometheus
text): ordo_gpu_resident_evicted{resident="llamacpp"} is 1 while a render has evicted
the GPU chat model, and ordo_gpu_chat_up is 0 while the chat engine is down. The parsed
state is cached for 5 s; an unreadable or unparseable metrics response is logged and
treated as "not evicted" (the fallback is allowed regardless).
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("gpu_lease_fallback_gate")

# ops-controller serves the GPU lease/eviction state unauthenticated (Prometheus scrapes
# it without a token), the same as /health.
METRICS_URL = os.environ.get("GPU_LEASE_GATE_METRICS_URL", "http://ops-controller:9000/metrics")
METRICS_TIMEOUT = float(os.environ.get("GPU_LEASE_GATE_TIMEOUT_SEC", "2"))
CACHE_TTL = float(os.environ.get("GPU_LEASE_GATE_CACHE_TTL_SEC", "5"))

# The two series ops-controller exports for the GPU chat service (ordo/control/metrics.py):
# one per resident (1 while stopped for a lease) and the /health liveness of the chat engine.
_EVICTED_RE = re.compile(r'^ordo_gpu_resident_evicted\{resident="llamacpp"\}\s+(\S+)', re.MULTILINE)
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
    (ordo_gpu_resident_evicted{resident="llamacpp"} 1) or the GPU chat service is down
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


class GpuLeaseFallbackGate(CustomLogger):
    """LiteLLM CustomLogger: always allows local-chat's CPU fallback and logs the
    GPU lease state for observability (see the module docstring)."""

    def __init__(self) -> None:
        # (expiry as time.monotonic(), parsed allowed state) of the last metrics read.
        self._cache: tuple[float, bool] | None = None

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
        # Operator policy (2026-10-06): a GPU failure must never kill a local-chat
        # consumer (Claude Code, Hermes, ...). Always allow the CPU fallback,
        # whether the GPU is evicted for a render, down, or merely returned an
        # error (a bad request, a transient 5xx, a mid-stream drop). The metrics
        # scrape is kept for observability — it logs the lease state — but no
        # longer gates the fallback. (Previously this raised ServiceUnavailable
        # when no lease was held, which turned a GPU 400 into a hard failure for
        # Claude Code instead of degrading to the CPU Qwen model.)
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
        (availability first) and the warning is logged.
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
            logger.warning(
                "gpu_lease_fallback_gate: cannot read %s (%s); allowing the CPU fallback",
                METRICS_URL,
                exc,
            )
            allowed = True
        self._cache = (now + CACHE_TTL, allowed)
        return allowed


# Module-level singleton, referenced from litellm_config.yaml (litellm_settings.callbacks).
gpu_lease_fallback_gate_instance = GpuLeaseFallbackGate()
