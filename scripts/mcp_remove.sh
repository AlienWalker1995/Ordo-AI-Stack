#!/usr/bin/env bash
# Remove an MCP server from the gateway. Run from repo root.
# Usage: ./scripts/mcp_remove.sh <server-name>
# Config is stored in out/mcp/servers.txt (mounted into ordo-mcp-gateway-1 at /mcp-config);
# gateway reloads in ~10s (no container restart). out/ is rendered by `ordo render` — this
# script edits the rendered file directly and does not survive a future re-render.

set -e

server="${1:?Usage: $0 <server-name>}"
base="${BASE_PATH:-$(pwd)}"
base="${base//\\/\/}"
out="${OUT_PATH:-$base/out}"
config_file="$out/mcp/servers.txt"

if [[ ! -f "$config_file" ]]; then
  echo "No MCP config found at $config_file" >&2
  exit 1
fi

current=$(tr -d '\r\n' < "$config_file" 2>/dev/null | tr ',' '\n' | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//')
servers=()
IFS=',' read -ra parts <<< "$current"
for p in "${parts[@]}"; do
  p="${p// /}"
  [[ -n "$p" && "$p" != "$server" ]] && servers+=("$p")
done

new_value=$(IFS=','; echo "${servers[*]}")
[[ -z "$new_value" ]] && new_value="n8n"

echo "$new_value" > "$config_file"

echo "Removed $server. Gateway will reload in ~10s (no container restart)."
