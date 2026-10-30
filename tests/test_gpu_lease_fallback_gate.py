"""Unit tests for the model-gateway GPU-lease fallback gate.

router_settings.fallbacks sends ANY error from the GPU deployment to the CPU model;
the gate (services/model-gateway/gpu_lease_fallback_gate.py) allows that hop, so a GPU
failure degrades to the CPU model instead of failing the caller, except when the GPU only
refused the request because its one-at-a-time queue was busy: that request is not moved
to the CPU (chat runs one request at a time, first come first served). It still reads
ops-controller's /metrics (ordo_gpu_resident_evicted{resident="inference-engine-gpu"},
ordo_gpu_chat_up) and logs the lease state for observability.

litellm is not a test dependency (huge tree); the gate only needs its CustomLogger
base class, so the module chain is stubbed before the file loads, the same way
test_throughput_callback.py does it.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]

_litellm = ModuleType("litellm")
_integrations = ModuleType("litellm.integrations")
_custom_logger = ModuleType("litellm.integrations.custom_logger")


_custom_logger.CustomLogger = object


class _ServiceUnavailableError(Exception):
    """Stands in for litellm.ServiceUnavailableError (keyword-only like the real one)."""

    def __init__(self, *, message, llm_provider, model):
        super().__init__(message)
        self.llm_provider = llm_provider
        self.model = model


_litellm.ServiceUnavailableError = _ServiceUnavailableError
_litellm.integrations = _integrations
_integrations.custom_logger = _custom_logger
sys.modules.setdefault("litellm", _litellm)
sys.modules.setdefault("litellm.integrations", _integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", _custom_logger)

_spec = importlib.util.spec_from_file_location(
    "gpu_lease_fallback_gate_under_test",
    REPO / "services" / "model-gateway" / "gpu_lease_fallback_gate.py",
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

CPU_MODEL = "qwen3.6-35b-a3b-ud-q4_k_m-cpu"

# One ops-controller /metrics scrape (the exact line shapes ordo/control/metrics.py emits).
METRICS_NORMAL = (
    "# HELP ordo_gpu_chat_up 1 when the GPU chat service answers /health with 200.\n"
    "# TYPE ordo_gpu_chat_up gauge\n"
    "ordo_gpu_chat_up 1\n"
    "# HELP ordo_gpu_resident_evicted 1 while a GPU resident is stopped for a lease.\n"
    "# TYPE ordo_gpu_resident_evicted gauge\n"
    'ordo_gpu_resident_evicted{resident="inference-engine-gpu"} 0\n'
    'ordo_gpu_resident_evicted{resident="inference-engine-embed"} 0\n'
)
METRICS_LEASED = METRICS_NORMAL.replace(
    'ordo_gpu_resident_evicted{resident="inference-engine-gpu"} 0',
    'ordo_gpu_resident_evicted{resident="inference-engine-gpu"} 1',
)
METRICS_CHAT_DOWN = METRICS_NORMAL.replace("ordo_gpu_chat_up 1", "ordo_gpu_chat_up 0")


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_client(monkeypatch, *, text: str | None = None, exc: Exception | None = None):
    """Point the gate's httpx.AsyncClient at a fake; returns the list of fetched URLs."""
    calls: list[str] = []

    class _FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url):
            calls.append(url)
            if exc is not None:
                raise exc
            return _FakeResponse(text or "")

    monkeypatch.setattr(gate.httpx, "AsyncClient", _FakeClient)
    return calls


def _hop_kwargs(key: str = "metadata", gpu_error: str | None = None) -> dict:
    """request_kwargs as the router assembles them on a fallback hop out of local-chat.

    gpu_error is the failed GPU attempt's message, which Router.log_retry leaves as the last
    previous_models breadcrumb.
    """
    meta: dict = {"original_model_group": "local-chat"}
    if gpu_error is not None:
        meta["previous_models"] = ({"exception_type": "RateLimitError", "exception_string": gpu_error},)
    return {key: meta}


# The messages NInfer's queue errors carry, as LiteLLM reports them (gateway log, 2026-10-29).
QUEUE_FULL = "litellm.RateLimitError: RateLimitError: OpenAIException - inference request queue is full"
QUEUE_EXPIRED = (
    "litellm.ServiceUnavailableError: ServiceUnavailableError: OpenAIException - "
    "inference request expired while waiting for admission"
)
BAD_REQUEST = "litellm.BadRequestError: OpenAIException - strict_tools_not_supported"


def _deployments() -> list:
    return [{"model_name": CPU_MODEL}]


# --- fallback_allowed: the pure metrics parsing ---------------------------------


def test_fallback_allowed_when_inference_engine_gpu_evicted():
    assert gate.fallback_allowed(METRICS_LEASED) is True


def test_fallback_allowed_when_chat_service_down():
    assert gate.fallback_allowed(METRICS_CHAT_DOWN) is True


def test_fallback_denied_when_gpu_up_and_resident():
    assert gate.fallback_allowed(METRICS_NORMAL) is False


def test_fallback_allowed_when_neither_line_present():
    # Nothing to judge by: fail open so a consumer is not locked out by a missing series.
    assert gate.fallback_allowed("# only other metrics here\nordo_container_running 1\n") is True


def test_fallback_allowed_on_garbage_text():
    assert gate.fallback_allowed("not a prometheus exposition at all") is True


# --- async_filter_deployments: the hook itself -----------------------------------


async def test_hook_passes_through_direct_local_chat(monkeypatch):
    calls = _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments("local-chat", _deployments(), [], request_kwargs={})
    assert out == _deployments()
    assert calls == []  # no metrics fetch for a non-fallback call


async def test_hook_passes_through_direct_cpu_call(monkeypatch):
    """A client that asks for the CPU model by name must still work, lease or no lease."""
    calls = _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs={})
    assert out == _deployments()
    assert calls == []


async def test_hook_allows_fallback_hop_when_gpu_up_and_not_leased(monkeypatch):
    """Operator policy: a GPU error (bad request, 5xx, mid-stream drop) must degrade to the
    CPU model, never fail the caller, even with the GPU up and no render holding the lease."""
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert out == _deployments()


async def test_hook_allows_fallback_hop_when_lease_held(monkeypatch):
    _install_client(monkeypatch, text=METRICS_LEASED)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert out == _deployments()


async def test_hook_allows_fallback_hop_when_chat_down(monkeypatch):
    _install_client(monkeypatch, text=METRICS_CHAT_DOWN)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert out == _deployments()


async def test_hook_uses_litellm_metadata_key(monkeypatch):
    """The router may carry the hop marker under litellm_metadata instead of metadata."""
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs("litellm_metadata"))
    assert out == _deployments()


async def test_hook_fails_open_when_metrics_fetch_raises(monkeypatch):
    _install_client(monkeypatch, exc=RuntimeError("ops-controller unreachable"))
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert out == _deployments()


async def test_hook_fails_open_on_http_error(monkeypatch):
    _install_client(monkeypatch, text="", exc=RuntimeError("HTTP 503"))
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert out == _deployments()


async def test_hook_caches_metrics_state_for_five_seconds(monkeypatch):
    calls = _install_client(monkeypatch, text=METRICS_LEASED)
    hook = gate.GpuLeaseFallbackGate()
    for _ in range(3):
        out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
        assert out == _deployments()
    assert len(calls) == 1  # the second and third hops read the 5 s cache, not the network


async def test_hook_logs_unreadable_metrics_at_most_once_per_minute(monkeypatch, caplog):
    """A sustained ops-controller outage must not log a 'cannot read' warning per cached
    failure (one per CACHE_TTL window): the warning is rate-limited to once per
    UNREADABLE_WARN_INTERVAL."""
    _install_client(monkeypatch, exc=RuntimeError("ops-controller unreachable"))
    monkeypatch.setattr(gate, "CACHE_TTL", 0.0)  # every hop re-fetches instead of reading the cache
    hook = gate.GpuLeaseFallbackGate()
    with caplog.at_level(logging.WARNING, logger="gpu_lease_fallback_gate"):
        for _ in range(3):
            out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
            assert out == _deployments()
    warnings = [r for r in caplog.records if "cannot read" in r.getMessage()]
    assert len(warnings) == 1


# --- the busy GPU queue is not a GPU failure --------------------------------------


async def test_hook_refuses_the_cpu_when_the_gpu_queue_is_full(monkeypatch):
    """Chat is one request at a time, first come first served: "queue is full" means the GPU is
    healthy and busy, so the request is not run beside it on the CPU."""
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    with pytest.raises(_ServiceUnavailableError, match="GPU chat queue is busy"):
        await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs(gpu_error=QUEUE_FULL))


async def test_hook_refuses_the_cpu_when_the_request_expired_in_the_gpu_queue(monkeypatch):
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    with pytest.raises(_ServiceUnavailableError):
        await hook.async_filter_deployments(
            CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs("litellm_metadata", gpu_error=QUEUE_EXPIRED)
        )


async def test_hook_still_allows_the_cpu_for_any_other_gpu_error(monkeypatch):
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    out = await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs(gpu_error=BAD_REQUEST))
    assert out == _deployments()


def test_queue_error_markers_match_only_queue_errors():
    assert gate.is_gpu_queue_error(QUEUE_FULL)
    assert gate.is_gpu_queue_error(QUEUE_EXPIRED)
    assert not gate.is_gpu_queue_error(BAD_REQUEST)
    assert not gate.is_gpu_queue_error("")
