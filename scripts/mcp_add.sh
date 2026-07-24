#!/usr/bin/env bash
# Add an MCP server to the gateway. Run from repo root.
# Usage: ./scripts/mcp_add.sh <server-name>
# Example: ./scripts/mcp_add.sh fetch
# Config is stored in out/mcp/servers.txt (mounted into ordo-mcp-gateway-1 at /mcp-config);
# gateway reloads in ~10s (no container restart). out/ is rendered by `ordo render` — this
# script edits the rendered file directly and does not survive a future re-render.

set -e

server="${1:?Usage: $0 <server-name>}"
base="${BASE_PATH:-$(pwd)}"
base="${base//\\/\/}"
out="${OUT_PATH:-$base/out}"
config_file="$out/mcp/servers.txt"

mkdir -p "$(dirname "$config_file")"
[[ ! -f "$config_file" ]] && echo "duckduckgo" > "$config_file"

current=$(tr -d '\r\n' < "$config_file" 2>/dev/null | tr ',' '\n' | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//')
servers=()
IFS=',' read -ra parts <<< "$current"
for p in "${parts[@]}"; do
  p="${p// /}"
  [[ -n "$p" ]] && servers+=("$p")
done

for s in "${servers[@]}"; do
  if [[ "$s" == "$server" ]]; then
    echo "Server '$server' is already enabled."
    exit 0
  fi
done

servers+=("$server")
echo "$(IFS=','; echo "${servers[*]}")" > "$config_file"

echo "Added $server. Gateway will reload in ~10s (no container restart)."
echo "$server is available at http://localhost:8811/mcp"
