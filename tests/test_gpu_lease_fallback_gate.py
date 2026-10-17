"""Unit tests for the model-gateway GPU-lease fallback gate.

router_settings.fallbacks sends ANY error from the GPU deployment to the CPU model;
the gate (services/model-gateway/gpu_lease_fallback_gate.py) always allows that hop, so
a GPU failure degrades to the CPU model instead of failing the caller. It still reads
ops-controller's /metrics (ordo_gpu_resident_evicted{resident="llamacpp"},
ordo_gpu_chat_up) and logs the lease state for observability. The same cached read lets its
pre-call hook answer the GPU pins with a 503 while the GPU chat model is evicted or down.

litellm is not a test dependency (huge tree); the gate only needs its CustomLogger
base class, so the module chain is stubbed before the file loads, the same way
test_throughput_callback.py does it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO = Path(__file__).resolve().parents[1]

_litellm = ModuleType("litellm")
_integrations = ModuleType("litellm.integrations")
_custom_logger = ModuleType("litellm.integrations.custom_logger")


_proxy = ModuleType("litellm.proxy")
_proxy_types = ModuleType("litellm.proxy._types")


class _ProxyException(Exception):
    """The fields of litellm.proxy._types.ProxyException the gate sets (code is a str there too)."""

    def __init__(self, message, type, param, code=None, headers=None):
        super().__init__(message)
        self.message = message
        self.type = type
        self.param = param
        self.code = str(code)


_custom_logger.CustomLogger = object
_proxy_types.ProxyException = _ProxyException
_litellm.integrations = _integrations
_litellm.proxy = _proxy
_integrations.custom_logger = _custom_logger
_proxy._types = _proxy_types
sys.modules.setdefault("litellm", _litellm)
sys.modules.setdefault("litellm.integrations", _integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", _custom_logger)
sys.modules.setdefault("litellm.proxy", _proxy)
sys.modules.setdefault("litellm.proxy._types", _proxy_types)

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


# --- async_pre_call_hook: the GPU pins answer 503 while the GPU is away ----------

GPU_DEPLOYMENT_NAME = "huihui-qwen3.8-27b-abliterated-nvfp4"


async def _pre_call(hook, model: str) -> None:
    await hook.async_pre_call_hook(None, None, {"model": model, "messages": []}, "acompletion")


async def _expect_503(hook, model: str):
    try:
        await _pre_call(hook, model)
    except gate.ProxyException as exc:
        assert exc.code == "503"
        return exc
    raise AssertionError(f"{model} was not refused")


async def test_pin_answers_503_while_evicted_for_a_lease(monkeypatch):
    _install_client(monkeypatch, text=METRICS_LEASED)
    exc = await _expect_503(gate.GpuLeaseFallbackGate(), "local-chat-gpu")
    assert exc.message == (
        "GPU chat model is evicted for a render (lease held); use local-chat for automatic CPU failover or wait"
    )
    assert exc.type == "service_unavailable"


async def test_pin_answers_503_while_chat_is_down(monkeypatch):
    _install_client(monkeypatch, text=METRICS_CHAT_DOWN)
    exc = await _expect_503(gate.GpuLeaseFallbackGate(), "local-chat-gpu")
    assert exc.message == "GPU chat model is down; use local-chat for automatic CPU failover or wait"


async def test_deployment_named_gpu_pin_is_refused_too(monkeypatch):
    monkeypatch.setenv("GPU_MODEL_NAME", GPU_DEPLOYMENT_NAME)
    _install_client(monkeypatch, text=METRICS_LEASED)
    await _expect_503(gate.GpuLeaseFallbackGate(), GPU_DEPLOYMENT_NAME)


async def test_pin_passes_while_gpu_is_up(monkeypatch):
    _install_client(monkeypatch, text=METRICS_NORMAL)
    await _pre_call(gate.GpuLeaseFallbackGate(), "local-chat-gpu")


async def test_failover_alias_and_cpu_pin_are_never_refused(monkeypatch):
    monkeypatch.setenv("GPU_MODEL_NAME", GPU_DEPLOYMENT_NAME)
    calls = _install_client(monkeypatch, text=METRICS_LEASED)
    hook = gate.GpuLeaseFallbackGate()
    for model in ("local-chat", "local-chat-cpu", CPU_MODEL, "local-embed"):
        await _pre_call(hook, model)
    assert calls == []  # not a GPU pin: no metrics fetch at all


async def test_pin_passes_when_metrics_are_unreadable(monkeypatch, caplog):
    calls = _install_client(monkeypatch, exc=RuntimeError("ops-controller unreachable"))
    hook = gate.GpuLeaseFallbackGate()
    times = iter([100.0, 106.0, 112.0, 170.0])
    # Replace the module's clock only (the event loop reads the real time.monotonic too).
    monkeypatch.setattr(gate, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with caplog.at_level("WARNING", logger="gpu_lease_fallback_gate"):
        for _ in range(4):
            await _pre_call(hook, "local-chat-gpu")
    assert len(calls) == 4  # one fetch per expired 5 s window
    warnings = [r for r in caplog.records if "cannot read" in r.getMessage()]
    assert len(warnings) == 2  # at 100 s and again at 170 s, not on every failed read


async def test_both_hooks_share_one_metrics_fetch_per_window(monkeypatch):
    calls = _install_client(monkeypatch, text=METRICS_LEASED)
    hook = gate.GpuLeaseFallbackGate()
    for _ in range(3):
        await _expect_503(hook, "local-chat-gpu")
    await hook.async_filter_deployments(CPU_MODEL, _deployments(), [], request_kwargs=_hop_kwargs())
    assert len(calls) == 1


def test_gpu_pin_models_never_include_the_failover_alias(monkeypatch):
    monkeypatch.setenv("GPU_MODEL_NAME", "local-chat")
    assert gate.gpu_pin_models() == {"local-chat-gpu"}
    monkeypatch.delenv("GPU_MODEL_NAME")
    assert gate.gpu_pin_models() == {"local-chat-gpu"}


# --- configuration: the metrics read timeout ---------------------------------------


def test_metrics_timeout_default_stays_above_the_chat_health_probe(monkeypatch):
    """The default must stay above ops-controller's 3 s /health probe (MetricsCollector._chat_up).

    /metrics is computed synchronously per scrape, so a hung (black-holed) chat engine makes
    every scrape take ~3 s. A shorter gate timeout gives up mid-scrape exactly in the "down"
    case this hook exists for, caches the fail-open None, and lets the pin wait out the hung
    engine instead of answering 503.
    """
    monkeypatch.delenv("GPU_LEASE_GATE_TIMEOUT_SEC", raising=False)
    # A fresh execution of the module source (the test loader keeps it out of sys.modules,
    # so importlib.reload would refuse it), with the env override unset: the default.
    assert _spec is not None
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)
    assert fresh.METRICS_TIMEOUT > 3.0
