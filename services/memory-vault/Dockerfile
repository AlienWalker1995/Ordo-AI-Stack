# syntax=docker/dockerfile:1
# Memory-Vault MCP server (compose service; stdio upstream bridged to streamable HTTP by mcp-proxy).
#
# Wraps @bitbonsai/mcpvault so the stack's mcp-gateway can spawn it as a sibling container and expose
# its markdown-vault tools (read_note, write_note, patch_note, search_notes, get_frontmatter,
# manage_tags, get_vault_stats, ...) to Hermes. mcpvault is pure-filesystem: it reads/writes the
# mounted Obsidian vault only, no network, no API keys.
#
# "Pin, don't float": the node base is digest-pinned and mcpvault is pinned to an EXACT npm version
# (not @latest). To bump, change MCPVAULT_VERSION (and re-resolve the base digest) deliberately.
#
# stdio hygiene: mcpvault emits JSON-RPC on stdout and only ever writes to stdout on `--version` /
# `--help` (early-exit paths we never hit) — normal operation keeps stdout clean, so nothing pollutes
# the gateway's stdio channel. No banner suppression needed.
FROM node@sha256:53ada149d435c38b14476cb57e4a7da73c15595aba79bd6971b547ceb6d018bf

ARG MCPVAULT_VERSION=0.12.1

LABEL org.opencontainers.image.title="mcpvault-mcp" \
      org.opencontainers.image.description="Markdown memory-vault MCP server (stdio) for the Ordo stack" \
      org.opencontainers.image.source="https://github.com/bitbonsai/mcpvault" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${MCPVAULT_VERSION}"

# Global install of the pinned mcpvault CLI (bin: `mcpvault` -> dist/server.js).
RUN npm install -g "@bitbonsai/mcpvault@${MCPVAULT_VERSION}" \
 && npm cache clean --force

# ── stdio -> streamable HTTP bridge (same recipe as services/codebase-memory/Dockerfile) ────
# mcpvault speaks stdio only. mcp-proxy holds ONE persistent child and serves stateless
# streamable HTTP on /mcp (LiteLLM re-initialises per operation: BerriAI/litellm #25128). Pinned.
ARG MCP_PROXY_VERSION=0.12.0
# mcp is pinned alongside: mcp-proxy 0.12.0 declares mcp>=1.17.0 unbounded and mcp 2.x breaks its imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir "mcp-proxy==${MCP_PROXY_VERSION}" "mcp==1.30.0" \
 && ln -s /opt/mcp-proxy/bin/mcp-proxy /usr/local/bin/mcp-proxy

# Run as the base image's non-root `node` user (uid 1000) so vault writes are owned by 1000, the
# same uid DATA_PATH uses, keeping the shared vault permission-clean.
USER node

# The vault is bind-mounted RW at /vault by compose (services/memory-vault/plugin.yaml).
EXPOSE 9000
# --pass-environment: mcp-proxy 0.12.0 otherwise starts the child with an EMPTY env (only HOME/PATH), so image/compose env would never reach it.
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "mcpvault", "/vault"]
