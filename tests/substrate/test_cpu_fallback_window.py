"""The CPU fallback serves the chat model's whole window, and the render proves it can.

`local-chat` fails over to `llamacpp-cpu` while a render holds the GPU. A failover that accepts less
than the primary rejects long conversations exactly when it is needed, so there is ONE window: the
GPU backend, the fallback, Hermes' compaction and the gateway all read the same resolved value.

That only holds while the fallback model can actually serve the window. Its catalog entry declares
the window it was trained for (`ctx_default`) and its KV rate (`kv_kb_per_token`), so the render
can check the window against it and say what the window costs in RAM. A window the fallback cannot
serve is a render error naming both windows and the fix, never a silently smaller fallback.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ordo.host import doctor
from ordo.render import engine
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0", "uuid": "GPU-aaaa"}],
                "ram_gb": 128, "cpu_cores": 48, "platform": "Linux"}

FULL_WINDOW = 262144
GIB = 1024 ** 3


def _render(window: int | None = None, plugins=("llamacpp-cpu",), site: dict | None = None):
    overrides = {"llamacpp": {"ctx_size": window}} if window else {}
    return render(Source.from_dict({"hardware": PROFILE_5090, "model": "qwen3.8-27b-uncensored-q6",
                                    "plugins": list(plugins), "overrides": overrides,
                                    "site": site or {}}), CATALOG, REGISTRY)


def _fallback_entry():
    return CATALOG.by_file(engine.CPU_FALLBACK_DEFAULT_FILE)


# --- the catalog data the check reads ------------------------------------------------------------------------------

def test_the_fallback_entry_declares_its_trained_window_and_kv_rate():
    entry = _fallback_entry()
    assert entry is not None and entry.id == "qwen3.6-35b-a3b-cpu-q4"
    # qwen35moe.context_length in the GGUF header
    assert entry.ctx_default == FULL_WINDOW
    # 10 full-attention layers (40 blocks, full_attention_interval 4) x 2 KV heads x 256 (K) + 256 (V),
    # at q8_0's 34 bytes per 32 values: 10 * 2 * 512 * 34 / 32 = 10,880 bytes = 10.625 KiB per token.
    assert entry.kv_kb_per_token == pytest.approx(10.625)


@pytest.mark.parametrize("path,pattern", [
    ("services/llamacpp-cpu/plugin.yaml", r"\$\{LLAMACPP_CPU_MODEL:-([^}]+)\}"),
    ("services/model-gateway/entrypoint.sh", r"\$\{LLAMACPP_CPU_MODEL:-([^}]+)\}"),
])
def test_the_render_checks_the_file_the_fallback_actually_loads(path, pattern):
    """The render resolves the fallback's catalog entry from the same default file the service loads
    and the gateway names; if one of them moved, the check would be about a different model."""
    defaults = set(re.findall(pattern, (ROOT / path).read_text(encoding="utf-8")))
    assert defaults == {engine.CPU_FALLBACK_DEFAULT_FILE}


# --- the full window -----------------------------------------------------------------------------------------------

def test_a_262k_window_reaches_every_consumer_including_the_fallback():
    rc = _render(FULL_WINDOW)
    derived = rc.manifest()["derived"]
    assert {str(v) for v in derived.values()} == {str(FULL_WINDOW)}
    assert rc.env["LLAMACPP_CPU_CTX"] == rc.env["LLAMACPP_CTX_SIZE"] == str(FULL_WINDOW)


def test_the_manifest_states_what_the_window_costs_the_fallback():
    fallback = _render(FULL_WINDOW).manifest()["cpu_fallback"]
    assert fallback["model"] == "qwen3.6-35b-a3b-cpu-q4"
    assert fallback["ctx_size"] == FULL_WINDOW
    assert fallback["trained_ctx"] == FULL_WINDOW
    # 262,144 tokens x 10,880 bytes = 2.66 GiB of KV; the weights are the pinned 22,134,528,992 bytes.
    assert fallback["kv_gb"] == pytest.approx(FULL_WINDOW * 10880 / GIB, abs=0.01)
    assert fallback["weights_gb"] == pytest.approx(22134528992 / GIB, abs=0.01)


def test_no_fallback_section_when_the_fallback_is_not_rendered():
    rc = _render(FULL_WINDOW, plugins=())
    assert "llamacpp-cpu" not in rc.plugins_enabled
    assert rc.manifest()["cpu_fallback"] is None


# --- a window the fallback cannot serve ----------------------------------------------------------------------------

def test_a_window_past_the_fallback_trained_window_is_a_render_error():
    with pytest.raises(ValueError) as e:
        _render(2 * FULL_WINDOW)
    message = str(e.value)
    assert f"{2 * FULL_WINDOW:,}" in message and f"{FULL_WINDOW:,}" in message
    assert "qwen3.6-35b-a3b-cpu-q4" in message
    assert "overrides.llamacpp.ctx_size" in message


def test_the_same_window_renders_when_no_fallback_is_enabled():
    rc = _render(2 * FULL_WINDOW, plugins=())
    assert rc.ctx_size == 2 * FULL_WINDOW


def test_an_uncatalogued_fallback_model_is_a_visible_warning():
    rc = _render(FULL_WINDOW, site={"LLAMACPP_CPU_MODEL": "Something-Else.gguf"})
    assert any("Something-Else.gguf" in w and "not in the catalog" in w for w in rc.warnings)
    assert rc.manifest()["cpu_fallback"] == {"model": None, "file": "Something-Else.gguf",
                                             "ctx_size": FULL_WINDOW, "trained_ctx": None,
                                             "kv_gb": None, "weights_gb": None}


# --- doctor shows both windows -------------------------------------------------------------------------------------

def test_doctor_bundle_carries_both_windows():
    bundle = doctor.collect_bundle(
        Source.from_dict({"hardware": PROFILE_5090, "model": "qwen3.8-27b-uncensored-q6",
                          "plugins": ["llamacpp-cpu"],
                          "overrides": {"llamacpp": {"ctx_size": FULL_WINDOW}}}), CATALOG, REGISTRY)
    assert bundle["sizing"]["ctx_size"] == FULL_WINDOW
    assert bundle["sizing"]["cpu_fallback"]["ctx_size"] == FULL_WINDOW
    assert bundle["sizing"]["cpu_fallback"]["trained_ctx"] == FULL_WINDOW


def test_doctor_window_line_names_both_windows_and_the_kv_cost():
    line = doctor.window_line(_render(FULL_WINDOW))
    assert line == "windows : chat 262,144; cpu-fallback 262,144 (trained 262,144; KV 2.7 GiB + weights 20.6 GiB)"


def test_doctor_window_line_without_a_fallback():
    assert doctor.window_line(_render(FULL_WINDOW, plugins=())) == "windows : chat 262,144; cpu-fallback not rendered"
