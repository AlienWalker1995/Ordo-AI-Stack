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


def _response(model: str = "local-chat", completion_tokens: int = 64):
    return {"model": model, "usage": {"completion_tokens": completion_tokens, "prompt_tokens": 10}}


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
    payload = tc._build_payload(kw, _response(completion_tokens=64), start, end)
    assert payload["model"] == "Qwen-GPU-Q6_K.gguf"
    assert payload["alias"] == "local-chat"
    assert payload["backend"] == "llamacpp"
    assert payload["output_tokens_per_sec"] == 32.0


def test_build_payload_still_none_without_completion_tokens():
    start = datetime(2026, 8, 17, 12, 0, 0)
    end = start + timedelta(seconds=2)
    kw = _kwargs(GPU_GGUF, "http://llamacpp:8080/v1")
    assert tc._build_payload(kw, _response(completion_tokens=0), start, end) is None
