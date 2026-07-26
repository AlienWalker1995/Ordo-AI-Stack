"""Wizard: plan proposal, capability + secret mapping, and a write -> render round-trip."""
from pathlib import Path

from ordo import wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HW_5090 = HardwareProfile.from_spec({"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128})
HW_CPU = HardwareProfile.from_spec({"gpus": [], "ram_gb": 16})


def test_plan_reflects_hardware():
    p = wizard.plan(CATALOG, REGISTRY, HW_5090)
    assert p.tier == "ultra"
    assert p.model_id == "huihui-qwen3.6-27b-q6"
    assert "song-gen" in p.plugins_available

    p_cpu = wizard.plan(CATALOG, REGISTRY, HW_CPU)
    assert "comfyui" not in p_cpu.plugins_available   # no GPU media on CPU
    assert "song-gen" not in p_cpu.plugins_available
    # light MCP tool servers still run on CPU (they're not GPU-bound)


def test_build_source_defaults_are_valid():
    src = wizard.build_source()
    assert src["agent"] == "hermes"                 # Hermes default
    assert src["model"] == "auto"
    assert src["plugins"] == "auto"
    assert "site" not in src                         # no edge access answers → no site block
    Source.from_dict(src)                            # must be a valid declarative source


def test_build_source_honors_answers():
    src = wizard.build_source({"tier": "medium", "model": "qwen2.5-7b-instruct-q4",
                               "plugins": ["comfyui"], "agent": "hermes"})
    assert src["tier"] == "medium" and src["plugins"] == ["comfyui"]
    Source.from_dict(src)


def test_build_source_folds_access_into_site():
    src = wizard.build_source({"caddy_hostname": "ordo.tail1234.ts.net", "caddy_bind": "0.0.0.0"})
    site = src["site"]
    assert site["CADDY_TAILNET_HOSTNAME"] == "ordo.tail1234.ts.net"
    assert site["CADDY_TAILNET_DOMAIN"] == "tail1234.ts.net"     # derived from the hostname
    assert site["CADDY_BIND"] == "0.0.0.0"
    Source.from_dict(src)                            # site must stay a valid source


def test_plugins_from_capabilities():
    all_ids = [p.id for p in REGISTRY.plugins]
    # every optional capability kept → "auto"
    assert wizard.plugins_from_capabilities(list(wizard.CAPABILITIES), all_ids) == "auto"
    assert wizard.plugins_from_capabilities(None, all_ids) == "auto"
    # drop image-video → its plugins gone, but the baseline (edge, dashboards) stays
    kept = set(wizard.CAPABILITIES) - {"image-video"}
    plugins = wizard.plugins_from_capabilities(list(kept), all_ids)
    assert isinstance(plugins, list)
    assert "comfyui" not in plugins and "song-gen" not in plugins
    assert "edge" in plugins and "hermes-dashboard" in plugins   # always-on baseline preserved


def test_resolve_secrets_generates_internal_and_blanks_external():
    required = ["LITELLM_MASTER_KEY", "OPS_CONTROLLER_TOKEN", "MCP_GATEWAY_TOKEN",
                "OAUTH2_PROXY_COOKIE_SECRET", "SEARXNG_SECRET", "N8N_API_KEY",
                "OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET",
                "HF_TOKEN", "TS_AUTHKEY", "GITHUB_PERSONAL_ACCESS_TOKEN"]
    values, generated, provided, blank = wizard.resolve_secrets(required)
    # every required key is present exactly once
    assert set(values) == set(required)
    # the six internal secrets are generated and non-empty
    for k in ("LITELLM_MASTER_KEY", "OPS_CONTROLLER_TOKEN", "MCP_GATEWAY_TOKEN",
              "OAUTH2_PROXY_COOKIE_SECRET", "SEARXNG_SECRET", "N8N_API_KEY"):
        assert k in generated and values[k]
    # unprovided external secrets are blank placeholders
    assert "HF_TOKEN" in blank and values["HF_TOKEN"] == ""
    assert not provided
    # oauth2-proxy cookie secret must decode to exactly 16/24/32 bytes
    import base64
    assert len(base64.urlsafe_b64decode(values["OAUTH2_PROXY_COOKIE_SECRET"])) in (16, 24, 32)


def test_resolve_secrets_honors_provided():
    values, generated, provided, blank = wizard.resolve_secrets(
        ["HF_TOKEN", "LITELLM_MASTER_KEY"], {"HF_TOKEN": "hf_abc123"})
    assert values["HF_TOKEN"] == "hf_abc123" and "HF_TOKEN" in provided
    assert "LITELLM_MASTER_KEY" in generated and values["LITELLM_MASTER_KEY"]


def test_run_headless_writes_valid_source_and_secrets(tmp_path):
    # the non-interactive install path: answers -> ordo.yaml + secrets.env, render must accept it
    result = wizard.run(CATALOG, REGISTRY, tmp_path, interactive=False,
                        answers={"caddy_hostname": "ordo.tail1234.ts.net", "caddy_bind": "0.0.0.0"})
    assert result.source_path.exists() and result.secrets_path.exists()

    # ordo.yaml renders end-to-end
    src = Source.load(result.source_path)
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.id and rc.ctx_size > 0

    # secrets.env carries EXACTLY the render's required key set, generated ones non-empty
    lines = [ln for ln in result.secrets_path.read_text().splitlines()
             if ln and not ln.startswith("#")]
    secrets = dict(ln.split("=", 1) for ln in lines)
    assert set(secrets) == set(rc.required_secrets)
    for k in result.generated_secret_keys:
        assert secrets[k], f"generated secret {k} should be non-empty"


def test_run_headless_full_answers_render(tmp_path):
    # a comprehensive answers dict (model + explicit plugins + access + provided secrets) renders
    kept = [c for c in wizard.CAPABILITIES if c != "voice"]
    all_ids = [p.id for p in REGISTRY.plugins]
    result = wizard.run(CATALOG, REGISTRY, tmp_path, interactive=False, answers={
        "model": "auto", "tier": "auto",
        "plugins": wizard.plugins_from_capabilities(kept, all_ids),
        "caddy_hostname": "ordo.tail1234.ts.net", "caddy_bind": "100.64.0.1",
        "secrets": {"HF_TOKEN": "hf_x", "OAUTH2_PROXY_CLIENT_ID": "cid"},
    })
    src = Source.load(result.source_path)
    render(src, CATALOG, REGISTRY)   # must not raise
    assert result.caddy_bind == "100.64.0.1"
    assert "HF_TOKEN" in result.provided_secret_keys
