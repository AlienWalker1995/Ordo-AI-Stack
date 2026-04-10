from __future__ import annotations

import json
from pathlib import Path

from openclaw.scripts import add_mcp_plugin_config


REPO_ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_CONFIG = REPO_ROOT / "data" / "openclaw" / "openclaw.json"
WORKSPACE_AGENTS = REPO_ROOT / "data" / "openclaw" / "workspace" / "AGENTS.md"


def test_openclaw_bridge_runtime_contract_is_stable() -> None:
    config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    providers = config["models"]["providers"]["gateway"]["models"]
    assert providers, "expected at least one gateway model configuration"
    assert providers[0]["contextWindow"] == 256000

    bridge = config["plugins"]["entries"]["openclaw-mcp-bridge"]["config"]
    assert bridge["flatTools"] is False
    assert bridge["injectSchemas"] is False

    local_tools = bridge["servers"]["local-tools"]
    assert local_tools["transport"] == "stdio"
    assert local_tools["toolPrefix"] == "local-tools"
    assert "read_file" in local_tools["flatToolAllowlist"]

    session_memory = config["hooks"]["internal"]["entries"]["session-memory"]
    assert session_memory["enabled"] is True
    assert session_memory["llmSlug"] is False

    assert config["agents"]["defaults"]["llm"]["idleTimeoutSeconds"] == 1800


def test_openclaw_workspace_agents_bootstrap_stays_under_limit() -> None:
    content = WORKSPACE_AGENTS.read_text(encoding="utf-8")
    assert len(content) < 3000, "AGENTS.md should fit OpenClaw bootstrap injection limits"


def test_openclaw_sync_script_preserves_selective_bridge_contract() -> None:
    data = {
        "hooks": {
            "internal": {
                "entries": {
                    "session-memory": {
                        "enabled": False,
                        "llmSlug": True,
                    }
                }
            }
        },
        "plugins": {
            "entries": {
                "openclaw-mcp-bridge": {
                    "config": {
                        "servers": {
                            "gateway": {
                                "url": "http://wrong-host/mcp",
                                "connectTimeoutMs": 1,
                                "requestTimeoutMs": 2,
                            }
                        },
                        "flatTools": True,
                        "injectSchemas": True,
                    }
                }
            }
        }
    }

    modified = add_mcp_plugin_config.normalize_mcp_bridge_servers(data)
    hook_modified = add_mcp_plugin_config.normalize_internal_hooks(data)
    assert modified is True
    assert hook_modified is True

    config = data["plugins"]["entries"]["openclaw-mcp-bridge"]["config"]
    assert config["flatTools"] is False
    assert config["injectSchemas"] is False
    assert config["servers"]["gateway"]["url"] == add_mcp_plugin_config._mcp_url

    local_tools = config["servers"]["local-tools"]
    assert local_tools["toolPrefix"] == "local-tools"
    assert local_tools["transport"] == "stdio"
    assert local_tools["flatToolAllowlist"] == ["read_file"]

    session_memory = data["hooks"]["internal"]["entries"]["session-memory"]
    assert session_memory["enabled"] is True
    assert session_memory["llmSlug"] is False

    data["agents"] = {"defaults": {"llm": {"idleTimeoutSeconds": 900}}}
    llm_modified = add_mcp_plugin_config.normalize_llm_idle_timeout(data)
    assert llm_modified is True
    assert data["agents"]["defaults"]["llm"]["idleTimeoutSeconds"] == 1800
