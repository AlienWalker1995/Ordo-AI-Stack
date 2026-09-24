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
HW_5090 = HardwareProfile.from_spec({"gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0"}],
                                     "ram_gb": 128})
HW_CPU = HardwareProfile.from_spec({"gpus": [], "ram_gb": 16})


def test_plan_reflects_hardware():
    p = wizard.plan(CATALOG, REGISTRY, HW_5090)
    assert p.tier == "ultra"
    # the catalog's top-ranked ultra model, and the operator's default as of 2026-09-20
    assert p.model_id == "qwen3.8-27b-turbo-fable-q6"
    assert "song-gen" in {p.id for p in REGISTRY.resolve("auto", HW_5090)[0]}

    cpu_plugins = {p.id for p in REGISTRY.resolve("auto", HW_CPU)[0]}
    assert "comfyui" not in cpu_plugins   # no GPU media on CPU
    assert "song-gen" not in cpu_plugins
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
    required = ["LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "LITELLM_DB_PASSWORD",
                "LITELLM_KEY_HERMES", "OPS_CONTROLLER_TOKEN",
                "OAUTH2_PROXY_COOKIE_SECRET", "SEARXNG_SECRET", "N8N_API_KEY",
                "OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET",
                "HF_TOKEN", "TS_AUTHKEY", "GITHUB_PERSONAL_ACCESS_TOKEN"]
    values, generated, provided, blank = wizard.resolve_secrets(required)
    assert set(values) == set(required)
    for k in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "LITELLM_DB_PASSWORD",
              "LITELLM_KEY_HERMES", "OPS_CONTROLLER_TOKEN",
              "OAUTH2_PROXY_COOKIE_SECRET", "SEARXNG_SECRET", "N8N_API_KEY"):
        assert k in generated and values[k]
    # LiteLLM keys (master, salt, every LITELLM_KEY_*) must carry the sk- prefix LiteLLM requires
    for k in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "LITELLM_KEY_HERMES"):
        assert values[k].startswith("sk-") and len(values[k]) >= 35
    assert "MCP_GATEWAY_TOKEN" not in wizard.SECRET_GENERATORS
    assert "HF_TOKEN" in blank and values["HF_TOKEN"] == ""
    assert not provided
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
    assert src.site["CADDY_BIND"] == "100.64.0.1"
    assert "HF_TOKEN" in result.provided_secret_keys


def test_hostname_error_accepts_valid_and_rejects_junk():
    assert wizard.hostname_error("ordo.tail1234.ts.net") is None
    assert wizard.hostname_error("") is not None                 # empty
    assert wizard.hostname_error("ordo") is not None             # not fully-qualified
    assert wizard.hostname_error("https://ordo.ts.net") is not None   # has a scheme
    assert wizard.hostname_error("ordo.ts.net:443") is not None       # has a port
    assert wizard.hostname_error("ordo .ts.net") is not None          # has a space


def test_parse_emails_splits_dedupes_and_preserves_order():
    assert wizard.parse_emails("a@x.io, b@y.io") == ["a@x.io", "b@y.io"]
    assert wizard.parse_emails("a@x.io  a@x.io\nb@y.io") == ["a@x.io", "b@y.io"]  # dedupe
    assert wizard.parse_emails("") == []
    assert wizard.parse_emails("  ,  ,  ") == []


def test_invalid_emails_flags_only_malformed():
    assert wizard.invalid_emails(["a@x.io", "b@y.co.uk"]) == []
    assert wizard.invalid_emails(["nope", "a@x.io", "also@bad"]) == ["nope", "also@bad"]


def test_run_records_the_host_paths_every_bind_needs(tmp_path):
    # Every host bind is ${BASE_PATH:?} / ${DATA_PATH:?} (fail loud), so the source `ordo init`
    # writes must carry both, as absolute host paths, or the first `up` refuses to start.
    repo = tmp_path / "repo"
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False, answers={},
                        host_root=repo)
    site = Source.load(result.source_path).site
    assert site["BASE_PATH"] == repo.as_posix()
    assert site["DATA_PATH"] == f"{repo.as_posix()}/data"


def test_run_keeps_host_paths_the_operator_chose(tmp_path):
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False,
                        answers={"site": {"DATA_PATH": "/srv/ordo-data"}}, host_root=tmp_path / "repo")
    site = Source.load(result.source_path).site
    assert site["DATA_PATH"] == "/srv/ordo-data"
    assert site["BASE_PATH"] == (tmp_path / "repo").as_posix()


def test_run_defaults_the_memory_vault_under_data(tmp_path):
    # memory-vault cannot run without MEMORY_VAULT_PATH; a local vault under data/ is the default.
    repo = tmp_path / "repo"
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False, answers={},
                        host_root=repo)
    assert Source.load(result.source_path).site["MEMORY_VAULT_PATH"] == f"{repo.as_posix()}/data/memory-vault"


def test_run_keeps_the_vault_the_operator_chose(tmp_path):
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False,
                        answers={"site": {"MEMORY_VAULT_PATH": "/srv/vault"}}, host_root=tmp_path / "repo")
    assert Source.load(result.source_path).site["MEMORY_VAULT_PATH"] == "/srv/vault"


def test_run_leaves_edge_out_of_an_explicit_list_when_the_front_door_is_blank(tmp_path):
    # A partial capability selection writes an explicit plugins list. With the front-door step left
    # blank, the edge has no CADDY_* keys, so it stays out of the list (a render would refuse it).
    kept = [c for c in wizard.CAPABILITIES if c != "voice"]
    all_ids = [p.id for p in REGISTRY.plugins]
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False, answers={
        "plugins": wizard.plugins_from_capabilities(kept, all_ids)}, host_root=tmp_path / "repo")
    src = Source.load(result.source_path)
    assert "edge" not in src.plugins
    assert "memory-vault" in src.plugins            # its vault path defaulted under data/
    note = next(w for w in result.warnings if "'edge'" in w)
    assert "CADDY_BIND" in note
    render(src, CATALOG, REGISTRY)                  # must not raise
