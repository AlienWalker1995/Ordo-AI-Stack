# syntax=docker/dockerfile:1
# Codebase-Memory MCP server (compose service; stdio upstream bridged to streamable HTTP by mcp-proxy).
#
# Wraps the upstream DeusData/codebase-memory-mcp release binary so the stack's
# mcp-gateway can spawn it as a sibling container and expose its code knowledge
# graph tools (search_graph, trace_path, get_architecture, get_code_snippet, ...)
# to Hermes. The binary bundles its embedding model (nomic-embed-code) and runs
# 100% offline — no API keys, no network at runtime (compose keeps it on the
# internal MCP network only).
#
# We use the *portable* (statically linked) release so there is no glibc coupling
# to the base image. Pinned + checksum-verified per the stack's "pin, don't float"
# rule: to bump, change CBM_VERSION and CBM_SHA256 together (sha256 comes from the
# release's checksums.txt for codebase-memory-mcp-linux-${CBM_ARCH}-portable.tar.gz).
# debian:bookworm-slim
FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ARG CBM_VERSION=v0.8.1
ARG CBM_ARCH=amd64
ARG CBM_SHA256=6ab87a6c05d049dde57700803ca0ab4199fcf25973a0606618af0fcee73f5abd

LABEL org.opencontainers.image.title="codebase-memory-mcp" \
      org.opencontainers.image.description="Code knowledge-graph MCP server (stdio) for the Ordo stack" \
      org.opencontainers.image.source="https://github.com/DeusData/codebase-memory-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${CBM_VERSION}"

# curl + ca-certificates are needed only at build time to fetch + verify the release.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    url="https://github.com/DeusData/codebase-memory-mcp/releases/download/${CBM_VERSION}/codebase-memory-mcp-linux-${CBM_ARCH}-portable.tar.gz"; \
    curl -fsSL -o /tmp/cbm.tar.gz "$url"; \
    echo "${CBM_SHA256}  /tmp/cbm.tar.gz" | sha256sum -c -; \
    mkdir -p /usr/local/share/codebase-memory-mcp; \
    tar -xzf /tmp/cbm.tar.gz -C /usr/local/share/codebase-memory-mcp; \
    install -m 0755 /usr/local/share/codebase-memory-mcp/codebase-memory-mcp /usr/local/bin/codebase-memory-mcp; \
    rm -f /tmp/cbm.tar.gz

# Index cache (SQLite) lives on a named volume mounted here by compose; persists across restarts.
ENV CBM_CACHE_DIR=/cache
ENV CBM_LOG_LEVEL=error

# ── stdio -> streamable HTTP bridge (the ONE recipe for stdio-only MCP servers) ─────────────
# The upstream binary speaks stdio only. mcp-proxy holds ONE persistent stdio child for the life
# of the process and serves stateless streamable HTTP on /mcp (+ GET /status). Stateless matters:
# LiteLLM's outbound MCP client re-initialises per operation (BerriAI/litellm #25128). Pinned.
ARG MCP_PROXY_VERSION=0.12.0
# mcp is pinned alongside: mcp-proxy 0.12.0 declares mcp>=1.17.0 unbounded and mcp 2.x breaks its imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/mcp-proxy \
 && /opt/mcp-proxy/bin/pip install --no-cache-dir "mcp-proxy==${MCP_PROXY_VERSION}" "mcp==1.30.0" \
 && ln -s /opt/mcp-proxy/bin/mcp-proxy /usr/local/bin/mcp-proxy
EXPOSE 9000
# --pass-environment: mcp-proxy 0.12.0 otherwise starts the child with an EMPTY env (only HOME/PATH), so CBM_*/vault settings would never reach it.
CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "codebase-memory-mcp"]
