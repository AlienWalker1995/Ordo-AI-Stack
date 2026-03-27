#!/usr/bin/env sh
# Wrapper that runs MCP gateway and reloads when config changes (no container restart).
# Reads servers from /mcp-config/servers.txt (comma-separated).
# When servers.txt is empty and registry.json exists, seeds from registry server keys.
# Repo copy: mcp/gateway/gateway-wrapper.sh (image: mcp/Dockerfile).

CONFIG_FILE="${MCP_CONFIG_FILE:-/mcp-config/servers.txt}"
REGISTRY_FILE="$(dirname "$CONFIG_FILE")/registry.json"
PORT="${MCP_GATEWAY_PORT:-8811}"
GATEWAY_BIN="/docker-mcp"

# Ensure config exists with default
mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "duckduckgo,n8n,tavily,comfyui" > "$CONFIG_FILE"
fi

read_servers() {
  content=$(tr -d '\r\n' < "$CONFIG_FILE" 2>/dev/null | tr ',' '\n' | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//')
  if [ -z "$content" ] && [ -f "$REGISTRY_FILE" ] && command -v jq >/dev/null 2>&1; then
    content=$(jq -r '.servers | keys | join(",")' "$REGISTRY_FILE" 2>/dev/null)
  fi
  printf '%s' "${content:-duckduckgo,n8n,tavily,comfyui}"
}

resolve_registry_custom() {
  reg_dir="$(dirname "$CONFIG_FILE")"
  src="$reg_dir/registry-custom.yaml"
  dst="$reg_dir/registry-custom.docker.yaml"
  if [ ! -f "$src" ]; then
    return 0
  fi
  # Inject secrets from mcp-gateway environment (same .env as stack).
  if command -v sed >/dev/null 2>&1; then
    sed -e "s|PLACEHOLDER_OPS_CONTROLLER_TOKEN|${OPS_CONTROLLER_TOKEN:-}|g" \
        -e "s|PLACEHOLDER_TAVILY_API_KEY|${TAVILY_API_KEY:-}|g" "$src" >"$dst"
  else
    cp "$src" "$dst"
  fi
}

start_gateway() {
  servers=$(read_servers)
  servers=${servers:-duckduckgo,n8n,tavily,comfyui}
  echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Starting gateway with servers: $servers"
  extra=""
  resolve_registry_custom
  reg_dir="$(dirname "$CONFIG_FILE")"
  # Must be --additional-catalog (not --additional-registry): when --servers is set, the gateway
  # does not read registry.yaml paths for server definitions — only catalog merges apply.
  [ -f "$reg_dir/registry-custom.docker.yaml" ] && extra="--additional-catalog $reg_dir/registry-custom.docker.yaml"
  verbose=""
  if [ "${MCP_GATEWAY_VERBOSE:-0}" = "1" ] || [ "${MCP_GATEWAY_VERBOSE:-}" = "true" ]; then
    verbose="--verbose"
  fi
  # shellcheck disable=SC2086
  "$GATEWAY_BIN" gateway run --transport=streaming --port="$PORT" --servers="$servers" $extra $verbose &
  echo $!
}

pid=$(start_gateway)
last_content=$(read_servers)

while true; do
  sleep 10
  content=$(read_servers 2>/dev/null || echo "duckduckgo,n8n,tavily,comfyui")
  [ -z "$content" ] && content="duckduckgo,n8n,tavily,comfyui"
  if [ "$content" != "$last_content" ]; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Config changed. Reloading gateway..."
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    last_content="$content"
    pid=$(start_gateway)
  fi
  # Check if gateway died unexpectedly
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Gateway exited. Restarting..."
    last_content=""
    pid=$(start_gateway)
  fi
done
