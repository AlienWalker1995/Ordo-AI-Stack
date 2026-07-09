#!/bin/sh
# Start the codebase-memory 3D graph UI as a long-lived service, served under the
# /codebase-memory/ subpath by nginx (which rewrites the SPA's absolute paths).
#
# The upstream graph is IN-MEMORY and is populated ONLY by an explicit index call
# (POST /api/index {"root_path":...}); it is wiped on every container restart. So
# after any restart the repo is un-indexed until someone manually indexes it. To
# make the graph always populated, we fire a ONE-SHOT auto-index in the BACKGROUND
# once the UI's local API is answering — asynchronously, so it never blocks or
# delays the container becoming healthy (the healthcheck curls nginx immediately).
set -eu

CACHE_DIR="${CBM_CACHE_DIR:-/cache}"
mkdir -p "$CACHE_DIR"

# Root to auto-index on startup. CBM_* namespace (not a host-path env like CODE_ROOT,
# which the app ignores) to avoid the host-path-leak confusion. Default = the whole
# Ordo repo (incl. the v2/ tree), which the app auto-excludes binaries within.
CBM_AUTOINDEX_ROOT="${CBM_AUTOINDEX_ROOT:-/c/dev/ordo-ai-stack}"

# Background one-shot auto-index: wait for the UI's local API to answer, then issue a
# single index call for the root. Idempotent and safe if the API is briefly not-ready
# (retry a few times, then give up quietly). Never blocks the server/healthcheck.
if [ -n "$CBM_AUTOINDEX_ROOT" ]; then
    (
        i=0
        while [ "$i" -lt 60 ]; do
            if curl -fsS -o /dev/null "http://127.0.0.1:9749/api/index-status" 2>/dev/null; then
                echo "[autoindex] API ready; indexing ${CBM_AUTOINDEX_ROOT}" >&2
                curl -fsS -X POST "http://127.0.0.1:9749/api/index" \
                    -H "Content-Type: application/json" \
                    -d "{\"root_path\":\"${CBM_AUTOINDEX_ROOT}\"}" >&2 \
                    && echo "[autoindex] index requested for ${CBM_AUTOINDEX_ROOT}" >&2 \
                    || echo "[autoindex] index request failed (non-fatal)" >&2
                exit 0
            fi
            i=$((i + 1))
            sleep 1
        done
        echo "[autoindex] API never became ready after 60s; skipping auto-index" >&2
    ) &
fi

# The UI binary is an MCP stdio server with the graph UI as a side thread (binds
# 127.0.0.1:9749). With no MCP client attached it would read EOF on stdin and exit,
# so keep stdin open (`tail -f /dev/null`) to hold the process — and the UI — up.
( tail -f /dev/null | codebase-memory-mcp --ui=true --port=9749 ) &

# nginx (foreground = container lifecycle) proxies /codebase-memory/* -> the UI on
# 127.0.0.1:9749 and rewrites /assets,/api,/rpc,/font-files to the subpath.
exec nginx -g 'daemon off;'
