# Extends docker/mcp-gateway with wrapper for runtime config reload (no container restart).
# docker/mcp-gateway:v2 (upstream ref — pinned to current registry digest)
FROM docker/mcp-gateway@sha256:101411b64e1346fb72905876a5fd1d5d0d7850c613374a0688bc20516620a9af
USER root
# Add jq for registry.json parsing (Alpine or Debian)
RUN apk add --no-cache jq curl 2>/dev/null || (apt-get update -qq && apt-get install -y -qq jq curl && rm -rf /var/lib/apt/lists/*) 2>/dev/null || true
COPY gateway/healthcheck.sh /mcp-scripts/healthcheck.sh
COPY gateway/gateway-wrapper.sh /mcp-scripts/gateway-wrapper.sh
ENTRYPOINT ["/bin/sh", "/mcp-scripts/gateway-wrapper.sh"]
