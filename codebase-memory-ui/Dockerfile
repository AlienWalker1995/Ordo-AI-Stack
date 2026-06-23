# syntax=docker/dockerfile:1
# Codebase-Memory 3D graph visualization UI (long-lived service).
#
# Runs the upstream UI-variant binary (`codebase-memory-mcp --ui=true`), which
# serves an interactive 3D knowledge-graph at :9749 as a thread alongside the MCP
# server. Three upstream quirks this image works around:
#   1. The UI HTTP server binds 127.0.0.1 ONLY (src/ui/httpd.c) — unreachable from
#      other containers. nginx (on 0.0.0.0:9750) proxies to 127.0.0.1:9749.
#   2. The UI is an absolute-asset SPA with no base-path option; nginx serves it
#      under /codebase-memory/ and sub_filter-rewrites its baked /assets,/api,/rpc
#      paths to that prefix so it can live on the shared :443 SSO origin (see
#      nginx.conf). No port, no collision with Open WebUI's root.
#   3. The process is an MCP stdio server; with no client it would hit EOF on stdin
#      and exit. The entrypoint keeps stdin open so the UI stays up.
#
# Shares the SAME index as the headless codebase-memory MCP via the
# `codebase-memory-cache` volume (CBM_CACHE_DIR=/cache), so it visualizes whatever
# Hermes has indexed.
#
# Pinned + checksum-verified (portable/static UI build). Bump CBM_VERSION +
# CBM_UI_SHA256 together (sha256 from the release checksums.txt,
# codebase-memory-mcp-ui-linux-${CBM_ARCH}-portable.tar.gz line).
FROM debian:bookworm-slim

ARG CBM_VERSION=v0.8.1
ARG CBM_ARCH=amd64
ARG CBM_UI_SHA256=74ed63e3af2d85bfd98cbcce7191295536d0a54b084cbc0b868d69f2272172da

LABEL org.opencontainers.image.title="codebase-memory-ui" \
      org.opencontainers.image.description="3D code knowledge-graph visualization UI for the Ordo stack" \
      org.opencontainers.image.source="https://github.com/DeusData/codebase-memory-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${CBM_VERSION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates nginx \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    url="https://github.com/DeusData/codebase-memory-mcp/releases/download/${CBM_VERSION}/codebase-memory-mcp-ui-linux-${CBM_ARCH}-portable.tar.gz"; \
    curl -fsSL -o /tmp/cbm.tar.gz "$url"; \
    echo "${CBM_UI_SHA256}  /tmp/cbm.tar.gz" | sha256sum -c -; \
    mkdir -p /usr/local/share/codebase-memory-ui; \
    tar -xzf /tmp/cbm.tar.gz -C /usr/local/share/codebase-memory-ui; \
    install -m 0755 /usr/local/share/codebase-memory-ui/codebase-memory-mcp /usr/local/bin/codebase-memory-mcp; \
    rm -f /tmp/cbm.tar.gz

COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV CBM_CACHE_DIR=/cache
ENV CBM_LOG_LEVEL=error

# nginx serves the rewritten SPA under /codebase-memory/ here; the UI itself listens
# on 127.0.0.1:9749 inside the container.
EXPOSE 9750

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
