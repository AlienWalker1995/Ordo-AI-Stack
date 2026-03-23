#!/usr/bin/env sh
# Wrapper that runs MCP gateway and reloads when config changes (no container restart).
# Reads servers from /mcp-config/servers.txt (comma-separated).
# When servers.txt is empty and registry.json exists, seeds from registry server keys.

CONFIG_FILE="${MCP_CONFIG_FILE:-/mcp-config/servers.txt}"
REGISTRY_FILE="$(dirname "$CONFIG_FILE")/registry.json"
PORT="${MCP_GATEWAY_PORT:-8811}"
GATEWAY_BIN="/docker-mcp"

# Ensure config exists with default
mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "n8n,playwright,comfyui" > "$CONFIG_FILE"
fi

read_servers() {
  content=$(tr -d '\r\n' < "$CONFIG_FILE" 2>/dev/null | tr ',' '\n' | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//')
  if [ -z "$content" ] && [ -f "$REGISTRY_FILE" ] && command -v jq >/dev/null 2>&1; then
    content=$(jq -r '.servers | keys | join(",")' "$REGISTRY_FILE" 2>/dev/null)
  fi
  printf '%s' "${content:-n8n,playwright,comfyui}"
}

resolve_registry_custom() {
  reg_dir="$(dirname "$CONFIG_FILE")"
  src="$reg_dir/registry-custom.yaml"
  dst="$reg_dir/registry-custom.docker.yaml"
  if [ ! -f "$src" ]; then
    return 0
  fi
  # Inject OPS_CONTROLLER_TOKEN from mcp-gateway environment (same .env as stack).
  if command -v sed >/dev/null 2>&1; then
    sed "s|PLACEHOLDER_OPS_CONTROLLER_TOKEN|${OPS_CONTROLLER_TOKEN:-}|g" "$src" >"$dst"
  else
    cp "$src" "$dst"
  fi
}

start_gateway() {
  servers=$(read_servers)
  servers=${servers:-n8n,playwright,comfyui}
  echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Starting gateway with servers: $servers"
  extra=""
  resolve_registry_custom
  reg_dir="$(dirname "$CONFIG_FILE")"
  [ -f "$reg_dir/registry-custom.docker.yaml" ] && extra="--additional-registry $reg_dir/registry-custom.docker.yaml"
  "$GATEWAY_BIN" gateway run --transport=streaming --port="$PORT" --servers="$servers" $extra &
  echo $!
}

pid=$(start_gateway)
last_content=$(read_servers)

while true; do
  sleep 10
  content=$(read_servers 2>/dev/null || echo "n8n,playwright,comfyui")
  [ -z "$content" ] && content="n8n,playwright,comfyui"
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
