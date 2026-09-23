"""Electricity-derived per-token cost for the local models (ordo.yaml `cost:`).

Covers the pure formula (`local_token_costs`), the `Source.cost` parsing/validation, the
env keys the render engine emits, and (end to end) that the model-gateway entrypoint's
placeholder substitution produces a `litellm_config.yaml` that still parses, with the cost
showing up in both `litellm_params` (what LiteLLM bills against) and `model_info` (what
`/model/info` and the UI display).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.render import local_token_costs, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
ENTRYPOINT = ROOT / "services" / "model-gateway" / "entrypoint.sh"
TEMPLATE = ROOT / "services" / "model-gateway" / "litellm_config.yaml"

PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128,
                "cpu_cores": 32, "platform": "Linux"}

EXAMPLE_COST = {
    "usd_per_kwh": 0.145,
    "inference_watts": 450,
    "prompt_tokens_per_second": 1200,
    "output_tokens_per_second": 43,
}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


# --- local_token_costs: the pure formula --------------------------------------------------

def test_empty_cost_is_free():
    assert local_token_costs({}) == ("0", "0")


def test_example_numbers_match_the_operator_worked_example():
    input_cost, output_cost = local_token_costs(EXAMPLE_COST)
    # usd_per_second = 450/1000 * 0.145 / 3600 = 1.8125e-05
    # input  = 1.8125e-05 / 1200 ≈ 1.510e-08
    # output = 1.8125e-05 / 43   ≈ 4.215e-07
    assert float(input_cost) == pytest.approx(1.5e-08, rel=0.01)
    assert float(output_cost) == pytest.approx(4.2e-07, rel=0.01)


def test_cost_strings_are_plain_decimal_not_scientific_notation():
    input_cost, output_cost = local_token_costs(EXAMPLE_COST)
    for text in (input_cost, output_cost):
        assert "e" not in text.lower(), f"{text!r} is scientific notation, not plain decimal"
        assert not text.endswith("."), f"{text!r} has a trailing bare dot"
        assert float(text) > 0


@pytest.mark.parametrize("missing_key", list(EXAMPLE_COST))
def test_missing_key_raises_naming_it(missing_key):
    cost = dict(EXAMPLE_COST)
    del cost[missing_key]
    with pytest.raises(ValueError, match=missing_key):
        local_token_costs(cost)


@pytest.mark.parametrize("zeroed_key", list(EXAMPLE_COST))
def test_zero_value_raises_naming_it(zeroed_key):
    cost = dict(EXAMPLE_COST)
    cost[zeroed_key] = 0
    with pytest.raises(ValueError, match=zeroed_key):
        local_token_costs(cost)


@pytest.mark.parametrize("negative_key", list(EXAMPLE_COST))
def test_negative_value_raises_naming_it(negative_key):
    cost = dict(EXAMPLE_COST)
    cost[negative_key] = -1
    with pytest.raises(ValueError, match=negative_key):
        local_token_costs(cost)


def test_unknown_key_raises_naming_it():
    cost = dict(EXAMPLE_COST)
    cost["gpu_count"] = 2
    with pytest.raises(ValueError, match="gpu_count"):
        local_token_costs(cost)


# --- Source.cost parsing ---------------------------------------------------------------------

def test_source_defaults_cost_to_empty_dict():
    s = Source.from_dict({})
    assert s.cost == {}


def test_source_round_trips_a_cost_block():
    s = Source.from_dict({"cost": EXAMPLE_COST})
    assert s.cost == EXAMPLE_COST


def test_source_rejects_non_mapping_cost():
    with pytest.raises(ValueError, match="cost"):
        Source.from_dict({"cost": [1, 2, 3]})


# --- render(): the env keys every consumer reads -----------------------------------------

def test_render_without_cost_emits_zero_env_keys():
    rc = render(_src(hardware=PROFILE_5090), CATALOG)
    assert rc.env["LOCAL_INPUT_COST_PER_TOKEN"] == "0"
    assert rc.env["LOCAL_OUTPUT_COST_PER_TOKEN"] == "0"


def test_render_with_cost_emits_both_env_keys():
    rc = render(_src(hardware=PROFILE_5090, cost=EXAMPLE_COST), CATALOG)
    assert float(rc.env["LOCAL_INPUT_COST_PER_TOKEN"]) == pytest.approx(1.5e-08, rel=0.01)
    assert float(rc.env["LOCAL_OUTPUT_COST_PER_TOKEN"]) == pytest.approx(4.2e-07, rel=0.01)


def test_render_with_invalid_cost_block_fails_loud():
    bad = dict(EXAMPLE_COST)
    bad["usd_per_kwh"] = 0
    with pytest.raises(ValueError, match="usd_per_kwh"):
        render(_src(hardware=PROFILE_5090, cost=bad), CATALOG)


# --- entrypoint placeholder substitution end to end ---------------------------------------

def _sh() -> str:
    for candidate in ("sh", "bash"):
        path = shutil.which(candidate)
        if path:
            return path
    pytest.skip("POSIX sh not available on PATH")
    return ""  # unreachable


def test_entrypoint_substitutes_cost_placeholders_into_valid_yaml(tmp_path):
    """Run the real entrypoint.sh substitution step (not a reimplementation) against the real
    template, with every placeholder given a sample value, and confirm: (a) the result still
    parses as YAML, and (b) local-chat carries the cost in both litellm_params and model_info."""
    template_posix = TEMPLATE.resolve().as_posix()
    out_path = tmp_path / "config.yaml"
    out_posix = out_path.as_posix()

    script = ENTRYPOINT.read_text(encoding="utf-8")
    # Point the sed pipeline at our real template and a scratch output file instead of the
    # container paths (/app/config.template.yaml, /tmp/config.yaml) - same technique as
    # tests/test_llamacpp_kv_cache_args.py's wrapper redirection.
    script = script.replace("/app/config.template.yaml", template_posix)
    script = script.replace("/tmp/config.yaml", out_posix)
    # Stop right after the sed pipeline - everything after it (copying the throughput
    # callback, merging MCP servers, exec'ing litellm) needs container-only files this test
    # doesn't have, and isn't what's under test here.
    marker = "cp /app/throughput_callback.py"
    assert marker in script, "entrypoint.sh shape changed - update this test's truncation point"
    script = script[: script.index(marker)]

    env = {
        **os.environ,
        "LITELLM_MASTER_KEY": "sk-" + "a" * 32,
        "LLAMACPP_CTX_SIZE": "131072",
        "LLAMACPP_N_PREDICT": "65536",
        "LLAMACPP_CPU_CTX": "131072",
        "LLAMACPP_MODEL": "Qwen3.8-27B-UD-Q6_K.gguf",
        "LLAMACPP_CPU_MODEL": "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
        "LLAMACPP_EMBED_MODEL": "nomic-embed-text-v1.5.Q4_K_M.gguf",
        "LLAMACPP_IMAGE": "ordo/llamacpp:pinned",
        "LLAMACPP_MMPROJ": "",
        "LOCAL_INPUT_COST_PER_TOKEN": "0.0000000151041667",
        "LOCAL_OUTPUT_COST_PER_TOKEN": "0.0000004215116279",
    }
    result = subprocess.run([_sh()], input=script, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"entrypoint substitution failed: {result.stderr}"

    rendered_text = out_path.read_text(encoding="utf-8")
    assert not re.search(r"__[A-Z_]+__", rendered_text), (
        f"unsubstituted placeholder(s) left in rendered config:\n{rendered_text}"
    )
    cfg = yaml.safe_load(rendered_text)
    local_chat = next(m for m in cfg["model_list"] if m["model_name"] == "local-chat")
    assert local_chat["litellm_params"]["input_cost_per_token"] == pytest.approx(0.0000000151041667)
    assert local_chat["litellm_params"]["output_cost_per_token"] == pytest.approx(0.0000004215116279)
    assert local_chat["model_info"]["input_cost_per_token"] == pytest.approx(0.0000000151041667)
    assert local_chat["model_info"]["output_cost_per_token"] == pytest.approx(0.0000004215116279)

    embed = next(m for m in cfg["model_list"] if m["model_name"] == "local-embed")
    assert embed["litellm_params"]["input_cost_per_token"] == pytest.approx(0.0000000151041667)
    assert embed["litellm_params"]["output_cost_per_token"] == 0
    assert embed["model_info"]["input_cost_per_token"] == pytest.approx(0.0000000151041667)
    assert embed["model_info"]["output_cost_per_token"] == 0
