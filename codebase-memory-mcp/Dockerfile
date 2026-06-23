# syntax=docker/dockerfile:1
# Codebase-Memory MCP server (gateway-spawned, stdio transport).
#
# Wraps the upstream DeusData/codebase-memory-mcp release binary so the stack's
# mcp-gateway can spawn it as a sibling container and expose its code knowledge
# graph tools (search_graph, trace_path, get_architecture, get_code_snippet, ...)
# to Hermes. The binary bundles its embedding model (nomic-embed-code) and runs
# 100% offline — no API keys, no network at runtime (we set disableNetwork in the
# gateway catalog entry).
#
# We use the *portable* (statically linked) release so there is no glibc coupling
# to the base image. Pinned + checksum-verified per the stack's "pin, don't float"
# rule: to bump, change CBM_VERSION and CBM_SHA256 together (sha256 comes from the
# release's checksums.txt for codebase-memory-mcp-linux-${CBM_ARCH}-portable.tar.gz).
FROM debian:bookworm-slim

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

# Index cache (SQLite) lives on a named volume mounted here by the gateway catalog
# entry; persists across container respawns.
ENV CBM_CACHE_DIR=/cache
# Keep logs on stderr / quiet so nothing pollutes the stdio JSON-RPC channel.
ENV CBM_LOG_LEVEL=error

# Default entry point = MCP stdio server (matches the upstream MCP client config,
# which launches the bare binary with no args).
CMD ["codebase-memory-mcp"]
