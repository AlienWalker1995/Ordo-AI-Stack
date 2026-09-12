"""Per-consumer LiteLLM virtual keys are DECLARED in manifests (`litellm_key:`) and RENDERED to
out/model-gateway/keys.json + secrets.env.example; grants name only enabled MCP servers."""
import json
from pathlib import Path

import pytest

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import Plugin, PluginRegistry
from ordo.render import litellm_model_names, render, render_litellm_keys

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-A"},
                   {"name": "GTX 1070", "vram_gb": 8, "uuid": "GPU-B"}], "ram_gb": 128}


def _full():
    return render(Source.from_dict({"hardware": P_DUAL, "model": "auto", "plugins": "auto"}), CATALOG, REGISTRY)


def test_key_env_names_derive_from_consumer_ids():
    keys = render_litellm_keys([("open-webui", {"models": ["local-chat"], "mcp_servers": []})], ["searxng"])
    assert keys == [{"env": "LITELLM_KEY_OPEN_WEBUI", "alias": "open-webui",
                     "models": ["local-chat"], "mcp_servers": []}]


def test_mcp_servers_all_expands_to_the_enabled_server_ids():
    keys = render_litellm_keys([("hermes", {"models": ["local-chat"], "mcp_servers": "all"})], ["b", "a"])
    assert keys[0]["mcp_servers"] == ["a", "b"]


def test_granting_an_unknown_server_fails_the_render():
    with pytest.raises(ValueError, match="nope"):
        render_litellm_keys([("x", {"models": ["local-chat"], "mcp_servers": ["nope"]})], ["searxng"])


# ── `models:` is fail-closed. LiteLLM reads an EMPTY models list as access to EVERY model, so an
#    omitted or empty list is a privilege escalation and a typo must not become a wildcard. ──
def test_model_names_come_from_the_litellm_config_model_list():
    names = litellm_model_names()
    assert "local-chat" in names and "local-embed" in names
    # __GPU_MODEL_NAME__ / __CPU_MODEL_NAME__ are entrypoint placeholders resolved from the deployed
    # GGUF filenames, so they are not render-known and must not be grantable.
    assert not any(n.startswith("__") for n in names)


def test_an_empty_or_absent_models_list_fails_the_render():
    for spec in ({"models": [], "mcp_servers": []}, {"mcp_servers": []}):
        with pytest.raises(ValueError, match="no models"):
            render_litellm_keys([("x", spec)], ["searxng"])


def test_granting_an_unknown_model_fails_the_render():
    with pytest.raises(ValueError, match="gpt-4o"):
        render_litellm_keys([("x", {"models": ["gpt-4o"], "mcp_servers": []})], ["searxng"])


def test_the_real_model_names_are_accepted():
    keys = render_litellm_keys([("x", {"models": ["local-chat", "local-embed"], "mcp_servers": []})],
                               ["searxng"])
    assert keys[0]["models"] == ["local-chat", "local-embed"]


def test_full_render_declares_hermes_open_webui_automation_keys(tmp_path):
    rc = _full()
    by_env = {k["env"]: k for k in rc.litellm_keys}
    # The grant names servers the way LiteLLM knows them (hyphen-free), NOT by hyphenated server_id:
    # LiteLLM expands an object_permission entry by exact server_id/alias/server_name match, so a
    # hyphenated entry resolves to nothing and the key silently loses that server.
    litellm_names = sorted(s["litellm_name"] for s in rc.mcp_servers)
    assert by_env["LITELLM_KEY_HERMES"]["mcp_servers"] == litellm_names
    assert not any("-" in s for s in by_env["LITELLM_KEY_HERMES"]["mcp_servers"])
    assert "memory_vault" in litellm_names
    assert by_env["LITELLM_KEY_HERMES"]["models"] == ["local-chat", "local-embed"]
    assert by_env["LITELLM_KEY_OPEN_WEBUI"]["mcp_servers"] == []
    assert by_env["LITELLM_KEY_AUTOMATION"]["models"] == ["local-chat"]
    for env in by_env:
        assert env in rc.required_secrets
    rc.write(tmp_path)
    data = json.loads((tmp_path / "model-gateway" / "keys.json").read_text())
    assert data == {"keys": rc.litellm_keys}


def test_plugin_manifest_parses_litellm_key():
    p = Plugin.from_dict({"id": "x", "litellm_key": {"models": ["local-chat"], "mcp_servers": []}})
    assert p.litellm_key == {"models": ["local-chat"], "mcp_servers": []}
    assert Plugin.from_dict({"id": "y"}).litellm_key == {}
