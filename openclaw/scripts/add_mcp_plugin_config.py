#!/usr/bin/env python3
"""Add MCP plugin config to openclaw.json and ensure the plugin manifest exists.

Run after openclaw-plugin-install.  The npm package (v0.1.0) omits
openclaw.plugin.json, so we create it here to unblock OpenClaw's plugin loader.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MCP_PLUGIN_ID = "openclaw-mcp-bridge"
MCP_PLUGIN_CONFIG = {
    "enabled": True,
    "config": {
        "servers": {
            "gateway": {"url": "http://mcp-gateway:8811/mcp"}
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


def _ensure_plugin_manifest() -> None:
    """Write openclaw.plugin.json if the npm package didn't ship one."""
    manifest_path = EXTENSIONS_DIR / "openclaw.plugin.json"
    if manifest_path.exists():
        return
    if not EXTENSIONS_DIR.exists():
        return
    try:
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

    if MCP_PLUGIN_ID in entries:
        return 0

    entries[MCP_PLUGIN_ID] = MCP_PLUGIN_CONFIG
    plugins["enabled"] = True

    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"add_mcp_plugin_config: added {MCP_PLUGIN_ID} to plugins.entries")
    except OSError as e:
        print(f"add_mcp_plugin_config: write failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
