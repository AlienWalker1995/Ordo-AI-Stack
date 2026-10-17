"""LiteLLM custom callback: local-chat's CPU fallback is always allowed, and the GPU pin
answers 503 at once while the GPU chat model is away.

Wired in `litellm_config.yaml` as:
    litellm_settings:
      callbacks: ["gpu_lease_fallback_gate.gpu_lease_fallback_gate_instance"]

router_settings.fallbacks sends ANY error from the GPU deployment (busy queue, 400, 500,
mid-stream) to the CPU model. Operator policy (2026-10-06): a GPU failure must never
kill a local-chat consumer (Claude Code, Hermes, ...), so the CPU fallback is ALWAYS
allowed — whether the GPU is evicted for a render, down, or merely returned an error.
This hook runs on the fallback hop and logs the lease state (read from ops-controller's
unauthenticated /metrics) for observability, but no longer denies the fallback.

The GPU pins (local-chat-gpu, and the deployment-named GPU alias the entrypoint exports as
GPU_MODEL_NAME) have no fallback of their own. While the GPU chat model is evicted for a
render or down, LiteLLM would answer them with a 500 "Connection error", which a client
such as Hermes retries as a server error instead of walking its own fallback chain. The
pre-call hook answers those requests with a 503 (overloaded) and a readable message
instead, so the client walks its fallback chain. local-chat and local-chat-cpu are never blocked.

The lease state is read from ops-controller's unauthenticated /metrics (Prometheus
text): ordo_gpu_resident_evicted{resident="llamacpp"} is 1 while a render has evicted
the GPU chat model, and ordo_gpu_chat_up is 0 while the chat engine is down. One cached
scrape (5 s) serves both hooks. An unreadable metrics response fails open: the fallback
is allowed and the pin is not blocked, and the warning is logged once per minute.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import ProxyException

logger = logging.getLogger("gpu_lease_fallback_gate")

# ops-controller serves the GPU lease/eviction state unauthenticated (Prometheus scrapes
# it without a token), the same as /health.
METRICS_URL = os.environ.get("GPU_LEASE_GATE_METRICS_URL", "http://ops-controller:9000/metrics")
METRICS_TIMEOUT = float(os.environ.get("GPU_LEASE_GATE_TIMEOUT_SEC", "2"))
CACHE_TTL = float(os.environ.get("GPU_LEASE_GATE_CACHE_TTL_SEC", "5"))
# An unreachable ops-controller is logged once per this many seconds, not once per scrape.
UNREADABLE_WARN_INTERVAL = 60.0

# The model groups that serve only the GPU chat model: the stable pin, plus the deployment-named
# alias (entrypoint.sh derives it from the weights file and exports it as GPU_MODEL_NAME).
GPU_PIN_ALIAS = "local-chat-gpu"

EVICTED_MESSAGE = "GPU chat model is evicted for a render (lease held); use local-chat for automatic CPU failover or wait"
DOWN_MESSAGE = "GPU chat model is down; use local-chat for automatic CPU failover or wait"

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


def gpu_unavailable_reason(metrics_text: str) -> str | None:
    """Why the GPU chat model cannot serve, from one ops-controller /metrics scrape, or None.

    Evicted for a lease takes precedence over down (an evicted engine is also down). A missing
    or malformed series is not evidence of anything, so it never blocks.
    """
    if _first_value(_EVICTED_RE, metrics_text) == 1.0:
        return EVICTED_MESSAGE
    if _first_value(_CHAT_UP_RE, metrics_text) == 0.0:
        return DOWN_MESSAGE
    return None


def gpu_pin_models() -> set[str]:
    """The model groups that route only to the GPU chat model (never local-chat itself)."""
    pins = {GPU_PIN_ALIAS}
    deployment_name = os.environ.get("GPU_MODEL_NAME", "").strip()
    if deployment_name and deployment_name != "local-chat":
        pins.add(deployment_name)
    return pins


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
    """LiteLLM CustomLogger: always allows local-chat's CPU fallback, logs the GPU lease
    state, and fails the GPU pins fast while the GPU is away (see the module docstring)."""

    def __init__(self) -> None:
        # (expiry as time.monotonic(), metrics text or None when unreadable) of the last read.
        self._cache: tuple[float, str | None] | None = None
        # time.monotonic() of the last "cannot read" warning, or None before the first.
        self._last_unreadable_warning: float | None = None

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> None:
        """Answer a GPU-pin request with a 503 while the GPU chat model is evicted or down.

        The proxy runs this once per client request (streaming or not) with the model group the
        caller named, before routing; router fallback hops and the background health probes do
        not pass through it. Raising ProxyException makes the proxy answer with that status and
        an OpenAI-shaped JSON error whose error.message is EVICTED_MESSAGE or DOWN_MESSAGE.
        """
        model = data.get("model") if isinstance(data, dict) else None
        if not isinstance(model, str) or model not in gpu_pin_models():
            return None
        metrics_text = await self._metrics_text()
        if metrics_text is None:
            return None  # fail open: no evidence the GPU is away
        reason = gpu_unavailable_reason(metrics_text)
        if reason is None:
            return None
        logger.info("gpu_lease_fallback_gate: answering 503 for %s: %s", model, reason)
        raise ProxyException(message=reason, type="service_unavailable", param="model", code=503)

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
        metrics_text = await self._metrics_text()
        allowed = metrics_text is None or fallback_allowed(metrics_text)
        logger.info(
            "gpu_lease_fallback_gate: allowing CPU fallback for %s (lease-allowed=%s)",
            model,
            allowed,
        )
        return healthy_deployments

    async def _metrics_text(self) -> str | None:
        """One ops-controller /metrics scrape, cached for CACHE_TTL seconds (both hooks share it).

        None when the metrics cannot be read; that result is cached too, so an unreachable
        ops-controller costs one fetch per window, and its warning is logged at most once per
        UNREADABLE_WARN_INTERVAL.
        """
        now = time.monotonic()
        cached = self._cache
        if cached is not None and now < cached[0]:
            return cached[1]
        metrics_text: str | None
        try:
            async with httpx.AsyncClient(timeout=METRICS_TIMEOUT) as client:
                response = await client.get(METRICS_URL)
                response.raise_for_status()
                metrics_text = response.text
        except Exception as exc:
            last = self._last_unreadable_warning
            if last is None or now - last >= UNREADABLE_WARN_INTERVAL:
                logger.warning(
                    "gpu_lease_fallback_gate: cannot read %s (%s); allowing the CPU fallback and the GPU pins",
                    METRICS_URL,
                    exc,
                )
                self._last_unreadable_warning = now
            metrics_text = None
        self._cache = (now + CACHE_TTL, metrics_text)
        return metrics_text


# Module-level singleton, referenced from litellm_config.yaml (litellm_settings.callbacks).
gpu_lease_fallback_gate_instance = GpuLeaseFallbackGate()
