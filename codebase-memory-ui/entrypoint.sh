#!/bin/sh
# Start the codebase-memory 3D graph UI as a long-lived service.
set -eu

CACHE_DIR="${CBM_CACHE_DIR:-/cache}"
mkdir -p "$CACHE_DIR"

# The UI HTTP server binds 127.0.0.1:9749 only. Bridge it to 0.0.0.0:9750 so the
# reverse proxy (Caddy) in another container can reach it.
socat TCP-LISTEN:9750,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9749 &

# The binary is an MCP stdio server with the UI as a side thread; with no MCP
# client attached it would read EOF on stdin and exit. Keep stdin open so the
# process (and the UI) stays alive. `tail -f /dev/null` never closes the pipe.
exec sh -c 'tail -f /dev/null | codebase-memory-mcp --ui=true --port=9749'
