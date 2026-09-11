#!/usr/bin/env python3
"""Merge the render-emitted LiteLLM `mcp_servers` fragment into the templated proxy config.

`ordo render` writes out/model-gateway/mcp_servers.yaml (mounted read-only at /config); the
entrypoint templates litellm_config.yaml into /tmp/config.yaml and then calls this to add the
`mcp_servers:` map. Kept as a tiny pure function + CLI so the substrate tests can exercise it
without LiteLLM. A missing fragment is a mount/render mismatch and exits 2 (fail loud, never
start with an empty tool set by accident).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def merge(config_text: str, fragment_text: str) -> str:
    """Return `config_text` with its `mcp_servers` key replaced by the fragment's mapping."""
    config = yaml.safe_load(config_text) or {}
    fragment = yaml.safe_load(fragment_text) or {}
    if not isinstance(fragment, dict) or "mcp_servers" not in fragment:
        raise ValueError("mcp fragment must be a mapping with an `mcp_servers` key")
    servers = fragment["mcp_servers"] or {}
    if not isinstance(servers, dict):
        raise ValueError("`mcp_servers` must be a mapping of server_id -> server config")
    config["mcp_servers"] = servers
    return yaml.safe_dump(config, sort_keys=False)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: merge_mcp_config.py <config.yaml> <mcp_servers.yaml>\n")
        return 2
    config_path, fragment_path = Path(argv[0]), Path(argv[1])
    if not fragment_path.is_file():
        sys.stderr.write(
            f"merge_mcp_config: fragment not found at {fragment_path} "
            "(is out/model-gateway mounted at /config? re-run `ordo render`)\n")
        return 2
    merged = merge(config_path.read_text(encoding="utf-8"), fragment_path.read_text(encoding="utf-8"))
    config_path.write_text(merged, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
