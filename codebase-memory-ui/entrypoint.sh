#!/bin/sh
# Start the codebase-memory 3D graph UI as a long-lived service, served under the
# /codebase-memory/ subpath by nginx (which rewrites the SPA's absolute paths).
set -eu

CACHE_DIR="${CBM_CACHE_DIR:-/cache}"
mkdir -p "$CACHE_DIR"

# The UI binary is an MCP stdio server with the graph UI as a side thread (binds
# 127.0.0.1:9749). With no MCP client attached it would read EOF on stdin and exit,
# so keep stdin open (`tail -f /dev/null`) to hold the process — and the UI — up.
( tail -f /dev/null | codebase-memory-mcp --ui=true --port=9749 ) &

# nginx (foreground = container lifecycle) proxies /codebase-memory/* -> the UI on
# 127.0.0.1:9749 and rewrites /assets,/api,/rpc,/font-files to the subpath.
exec nginx -g 'daemon off;'
