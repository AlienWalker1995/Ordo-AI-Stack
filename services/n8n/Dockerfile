# syntax=docker/dockerfile:1
# n8n MCP server (compose service; stdio upstream bridged to streamable HTTP by mcp-proxy).
#
# Wraps the pinned upstream czlonkowski/n8n-mcp image so LiteLLM's MCP gateway can reach its node,
# template and n8n-instance tools (search_nodes, get_node, search_templates, n8n_list_workflows,
# n8n_create_workflow, ...) from Hermes.
#
# Why the bridge and not the image's own HTTP mode: n8n-mcp's MCP_MODE=http transport is SESSION-ful
# (a bare tools/list answers `400 No valid session ID provided and not an initialize request`, and
# the image exposes no stateless switch). LiteLLM's outbound MCP client opens a fresh session per
# operation and does not carry Mcp-Session-Id (BerriAI/litellm #25128), so its calls would never
# reach the server. mcp-proxy holds ONE persistent stdio child and serves STATELESS streamable HTTP
# on /mcp, which is the shape LiteLLM needs.
#
# "Pin, don't float": the upstream image is pinned by tag AND digest; to bump, change both together.
FROM ghcr.io/czlonkowski/n8n-mcp:2.84.1@sha256:f8c62d1c0485791dbbbd7329dccf902de5f279894c5180876176b8f086e48244

LABEL org.opencontainers.image.title="n8n-mcp" \
      org.opencontainers.image.description="n8n node/template/instance MCP server (stdio) for the Ordo stack" \
      org.opencontainers.image.source="https://github.com/czlonkowski/n8n-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="2.84.1"

# ── stdio -> streamable HTTP bridge (same recipe as services/codebase-memory/Dockerfile) ────
# Alpine variant of the recipe: the base image is Alpine 3.24, so apk instead of apt-get.
ARG MCP_PROXY_VERSION=0.12.0
USER root
# mcp is pinned alongside: mcp-proxy 0.12.0 declares mcp>=1.17.0 unbounded and mcp 2.x breaks its imports.
RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir "mcp-proxy==${MCP_PROXY_VERSION}" "mcp==1.30.0" \
 && ln -s /opt/mcp-proxy/bin/mcp-proxy /usr/local/bin/mcp-proxy

# stdio is the image default; pinned here so the mode cannot drift with an upstream bump.
ENV MCP_MODE=stdio

# ENTRYPOINT is cleared deliberately. The upstream /usr/local/bin/docker-entrypoint.sh ends in a
# branch on MCP_MODE, and its stdio branch runs `exec node /app/dist/mcp/stdio-wrapper.js` WITHOUT
# forwarding "$@" - it would silently discard the mcp-proxy command below and we would get a bare
# stdio server with no HTTP listener. Nothing the entrypoint does is needed here: the node database
# it would seed (/app/data/nodes.db) is already baked into the image, and we run as `nodejs`
# directly so its root-only chown/su-exec path never applies.
ENTRYPOINT []

# Same non-root user the upstream image runs as; /app/data is owned by it.
USER nodejs
WORKDIR /app

EXPOSE 9000
# --pass-environment is load-bearing: mcp-proxy spawns the stdio child with an EMPTY environment
# unless told otherwise, and without N8N_API_URL / N8N_API_KEY the server registers only its 7
# documentation tools instead of the full 28 (the 21 n8n_* instance verbs disappear).
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "node", "dist/mcp/index.js"]
