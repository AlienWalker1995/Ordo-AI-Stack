# syntax=docker/dockerfile:1
# Memory-Vault MCP server (gateway-spawned, stdio transport).
#
# Wraps @bitbonsai/mcpvault so the stack's mcp-gateway can spawn it as a sibling container and expose
# its markdown-vault tools (read_note, write_note, patch_note, search_notes, get_frontmatter,
# manage_tags, get_vault_stats, ...) to Hermes. mcpvault is pure-filesystem: it reads/writes the
# mounted Obsidian vault only, no network, no API keys (we set disableNetwork in the gateway catalog).
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

# Run as the base image's non-root `node` user (uid 1000) so vault writes are owned by 1000 — the
# same uid DATA_PATH uses (and the operator's own files), keeping the shared vault permission-clean.
USER node

# The vault is bind-mounted here (RW) by the gateway catalog (volumes: PLACEHOLDER_MEMORY_VAULT_PATH
# :/vault). Pass it as mcpvault's positional vault-path arg so the server operates on it directly.
CMD ["mcpvault", "/vault"]
