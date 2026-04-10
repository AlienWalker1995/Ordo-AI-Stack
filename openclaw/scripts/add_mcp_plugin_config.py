#!/usr/bin/env python3
"""Add MCP plugin config to openclaw.json and ensure the plugin manifest exists.

Run after openclaw-plugin-install.  The npm package (v0.1.0) omits
openclaw.plugin.json, so we create it here to unblock OpenClaw's plugin loader.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

MCP_PLUGIN_ID = "openclaw-mcp-bridge"
_mcp_url = os.environ.get("MCP_GATEWAY_URL", "http://mcp-gateway:8811/mcp")
_connect_timeout_ms = int(os.environ.get("OPENCLAW_MCP_CONNECT_TIMEOUT_MS", "10000"))
_request_timeout_ms = int(os.environ.get("OPENCLAW_MCP_REQUEST_TIMEOUT_MS", "900000"))
_workspace_root = os.environ.get("OPENCLAW_WORKSPACE_ROOT", "/home/node/.openclaw/workspace")
MCP_PLUGIN_CONFIG = {
    "enabled": True,
    "config": {
        "servers": {
            # Single endpoint: Docker MCP Gateway (aggregates n8n, tavily, comfyui, …).
            "gateway": {
                "url": _mcp_url,
                "connectTimeoutMs": _connect_timeout_ms,
                "requestTimeoutMs": _request_timeout_ms,
            },
            "local-tools": {
                "url": "stdio://local",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", _workspace_root],
                "toolPrefix": "local-tools",
                "flatToolAllowlist": ["read_file"],
                "connectTimeoutMs": _connect_timeout_ms,
                "requestTimeoutMs": 30000,
            },
        },
        "debug": False,
        "flatTools": False,
        "injectSchemas": False,
    },
}

_llm_idle_timeout_seconds = 1800

SESSION_MEMORY_HOOK_CONFIG = {
    "enabled": True,
    "llmSlug": False,
}

PLUGIN_MANIFEST = {
    "id": MCP_PLUGIN_ID,
    "name": "MCP Bridge",
    "description": "Bridges MCP servers as native OpenClaw tools via streamable-http.",
    "configSchema": {
        "type": "object",
        "properties": {
            "servers": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "transport": {"type": "string"},
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "connectTimeoutMs": {"type": "number"},
                        "requestTimeoutMs": {"type": "number"},
                        "toolPrefix": {"type": "string"},
                        "flatToolAllowlist": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["url"],
                },
            },
            "debug": {"type": "boolean"},
            "flatTools": {"type": "boolean"},
            "injectSchemas": {"type": "boolean"},
        },
    },
}

EXTENSIONS_DIR = Path("/config/extensions/openclaw-mcp-bridge")


def normalize_mcp_bridge_servers(data: dict) -> bool:
    """Normalize MCP bridge config to the single Docker gateway with durable timeouts.

    ComfyUI and other catalog servers are reached through ``mcp-gateway``; do not
    configure a separate ``comfyui`` URL on the bridge.
    """
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        return False
    bridge = entries.get(MCP_PLUGIN_ID)
    if not isinstance(bridge, dict):
        return False
    cfg = bridge.get("config")
    if not isinstance(cfg, dict):
        return False
    servers = cfg.get("servers")
    if not isinstance(servers, dict):
        return False

    modified = False
    if "comfyui" in servers:
        del servers["comfyui"]
        modified = True
    gateway = servers.get("gateway")
    if isinstance(gateway, dict):
        if gateway.get("url") != _mcp_url:
            gateway["url"] = _mcp_url
            modified = True
        if gateway.get("connectTimeoutMs") != _connect_timeout_ms:
            gateway["connectTimeoutMs"] = _connect_timeout_ms
            modified = True
        if gateway.get("requestTimeoutMs") != _request_timeout_ms:
            gateway["requestTimeoutMs"] = _request_timeout_ms
            modified = True

    local_tools = servers.get("local-tools")
    desired_local_tools = MCP_PLUGIN_CONFIG["config"]["servers"]["local-tools"]
    if not isinstance(local_tools, dict):
        servers["local-tools"] = copy.deepcopy(desired_local_tools)
        modified = True
    else:
        for key, value in desired_local_tools.items():
            if local_tools.get(key) != value:
                local_tools[key] = copy.deepcopy(value)
                modified = True

    # Keep the bridge in selective mode: direct file reads through local-tools, everything else via gateway__call.
    if cfg.get("flatTools") is not False:
        cfg["flatTools"] = False
        modified = True
    # Keep prompt context compact by discovering tools on demand instead of injecting full schemas.
    if cfg.get("injectSchemas") is not False:
        cfg["injectSchemas"] = False
        modified = True
    return modified


def normalize_internal_hooks(data: dict) -> bool:
    """Keep session-memory enabled but disable LLM slug generation.

    /new and /reset should not block on an embedded helper run just to name the
    markdown memory file. The built-in fallback timestamp slug is sufficient.
    """
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    internal = hooks.setdefault("internal", {})
    if not isinstance(internal, dict):
        return False
    entries = internal.setdefault("entries", {})
    if not isinstance(entries, dict):
        return False

    modified = False
    session_memory = entries.get("session-memory")
    if not isinstance(session_memory, dict):
        entries["session-memory"] = copy.deepcopy(SESSION_MEMORY_HOOK_CONFIG)
        return True

    for key, value in SESSION_MEMORY_HOOK_CONFIG.items():
        if session_memory.get(key) != value:
            session_memory[key] = copy.deepcopy(value)
            modified = True
    return modified


def normalize_llm_idle_timeout(config: dict) -> bool:
    """Ensure llm.idleTimeoutSeconds is set to 1800 to survive GPU contention at cron start time.

    The default 900 s is too tight when llamacpp shares VRAM with ComfyUI — the model
    may not produce its first token within 900 s if the GPU is occupied.
    """
    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        return False
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        return False
    llm = defaults.setdefault("llm", {})
    if not isinstance(llm, dict):
        return False
    if llm.get("idleTimeoutSeconds") != _llm_idle_timeout_seconds:
        llm["idleTimeoutSeconds"] = _llm_idle_timeout_seconds
        return True
    return False


def _ensure_plugin_manifest() -> None:
    """Write or refresh openclaw.plugin.json so stale installs pick up schema changes."""
    manifest_path = EXTENSIONS_DIR / "openclaw.plugin.json"
    try:
        EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(PLUGIN_MANIFEST, indent=2)
        existing = None
        if manifest_path.exists():
            existing = manifest_path.read_text(encoding="utf-8")
        if existing == rendered:
            return
        manifest_path.write_text(rendered, encoding="utf-8")
        action = "updated" if existing is not None else "created"
        print(f"add_mcp_plugin_config: {action} {manifest_path}")
    except OSError as e:
        print(f"add_mcp_plugin_config: manifest write failed: {e}", file=sys.stderr)


def main() -> int:
    _ensure_plugin_manifest()

    config_path = Path("/config/openclaw.json")
    if not config_path.exists():
        return 0

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"add_mcp_plugin_config: skip (read error): {e}", file=sys.stderr)
        return 0

    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        return 0
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        return 0

    modified = False
    if MCP_PLUGIN_ID not in entries:
        entries[MCP_PLUGIN_ID] = copy.deepcopy(MCP_PLUGIN_CONFIG)
        plugins["enabled"] = True
        modified = True
    if normalize_mcp_bridge_servers(data):
        modified = True
    if normalize_internal_hooks(data):
        modified = True
    if normalize_llm_idle_timeout(data):
        modified = True

    if not modified:
        return 0

    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"add_mcp_plugin_config: updated {MCP_PLUGIN_ID} (single MCP gateway)")
    except OSError as e:
        print(f"add_mcp_plugin_config: write failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
