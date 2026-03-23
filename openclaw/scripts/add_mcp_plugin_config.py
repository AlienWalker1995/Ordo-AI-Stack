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
MCP_PLUGIN_CONFIG = {
    "enabled": True,
    "config": {
        "servers": {
            # Single endpoint: Docker MCP Gateway (aggregates n8n, playwright, comfyui, …).
            "gateway": {"url": _mcp_url},
        },
        "debug": False,
    },
}

PLUGIN_MANIFEST = {
    "id": MCP_PLUGIN_ID,
    "name": "MCP Bridge",
    "description": "Bridges MCP servers as native OpenClaw tools via streamable-http.",
    "configSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "servers": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
            "debug": {"type": "boolean"},
        },
    },
}

EXTENSIONS_DIR = Path("/config/extensions/openclaw-mcp-bridge")


def normalize_mcp_bridge_servers(data: dict) -> bool:
    """Remove a legacy second MCP URL so only the Docker MCP gateway remains.

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
    return modified


def _ensure_plugin_manifest() -> None:
    """Write openclaw.plugin.json if missing. Creates the extensions dir if needed."""
    manifest_path = EXTENSIONS_DIR / "openclaw.plugin.json"
    if manifest_path.exists():
        return
    try:
        EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(PLUGIN_MANIFEST, indent=2), encoding="utf-8")
        print(f"add_mcp_plugin_config: created missing {manifest_path}")
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
