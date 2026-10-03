"""Unit tests for the model-gateway GPU-lease fallback gate.

router_settings.fallbacks sends ANY error from the GPU deployment to the CPU model;
the gate (services/model-gateway/gpu_lease_fallback_gate.py) denies that hop unless
ops-controller's /metrics says the GPU chat model is evicted for a lease
(ordo_gpu_resident_evicted{resident="llamacpp"} 1) or the GPU chat service is down
(ordo_gpu_chat_up 0). A busy GPU or a bad request must fail loudly, not degrade to
the CPU model.

litellm is not a test dependency (huge tree); the gate only needs its CustomLogger
base class and its ServiceUnavailableError, so the module chain is stubbed before
the file loads, the same way test_throughput_callback.py does it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]

_litellm = ModuleType("litellm")
_integrations = ModuleType("litellm.integrations")
_custom_logger = ModuleType("litellm.integrations.custom_logger")


class _ServiceUnavailableError(Exception):
    """Stand-in for litellm.ServiceUnavailableError (message/llm_provider/model kwargs)."""

    def __init__(self, message=None, llm_provider=None, model=None, **kwargs):
        super().__init__(message)
        self.message = message
        self.llm_provider = llm_provider
        self.model = model


_custom_logger.CustomLogger = object
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
    'ordo_gpu_resident_evicted{resident="llamacpp"} 0\n'
    'ordo_gpu_resident_evicted{resident="llamacpp-embed"} 0\n'
)
METRICS_LEASED = METRICS_NORMAL.replace(
    'ordo_gpu_resident_evicted{resident="llamacpp"} 0',
    'ordo_gpu_resident_evicted{resident="llamacpp"} 1',
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


def _hop_kwargs(key: str = "metadata") -> dict:
    """request_kwargs as the router assembles them on a fallback hop out of local-chat."""
    return {key: {"original_model_group": "local-chat"}}


def _deployments() -> list:
    return [{"model_name": CPU_MODEL}]


# --- fallback_allowed: the pure metrics parsing ---------------------------------


def test_fallback_allowed_when_llamacpp_evicted():
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


async def test_hook_raises_on_fallback_hop_when_gpu_up_and_not_leased(monkeypatch):
    _install_client(monkeypatch, text=METRICS_NORMAL)
    hook = gate.GpuLeaseFallbackGate()
    with pytest.raises(_ServiceUnavailableError) as excinfo:
        await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert excinfo.value.message == gate.DENY_MESSAGE
    assert excinfo.value.model == CPU_MODEL


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
    with pytest.raises(_ServiceUnavailableError):
        await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs("litellm_metadata"))


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
