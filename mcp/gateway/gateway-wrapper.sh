#!/usr/bin/env sh
# Wrapper that runs MCP gateway and reloads when config changes (no container restart).
# Reads servers from /mcp-config/servers.txt (comma-separated).
# When servers.txt is empty and registry.json exists, seeds from registry server keys.
# Repo copy: mcp/gateway/gateway-wrapper.sh (image: mcp/Dockerfile).

CONFIG_FILE="${MCP_CONFIG_FILE:-/mcp-config/servers.txt}"
REGISTRY_FILE="$(dirname "$CONFIG_FILE")/registry.json"
PORT="${MCP_GATEWAY_PORT:-8811}"
GATEWAY_BIN="/docker-mcp"
POLL_SEC="${MCP_GATEWAY_POLL_SEC:-5}"
RELOAD_DEBOUNCE_SEC="${MCP_GATEWAY_RELOAD_DEBOUNCE_SEC:-20}"

# Bridge Docker secrets *_FILE pointers to plaintext env vars. The gateway
# itself + its sed substitution into registry-custom.docker.yaml expect the
# canonical env names (TAVILY_API_KEY, GITHUB_PERSONAL_ACCESS_TOKEN). The
# gateway also propagates these to spawned MCP-server containers (e.g. the
# `tavily` MCP that reads TAVILY_API_KEY directly).
if [ -n "${TAVILY_API_KEY_FILE:-}" ] && [ -f "$TAVILY_API_KEY_FILE" ]; then
    TAVILY_API_KEY="$(cat "$TAVILY_API_KEY_FILE")"
    export TAVILY_API_KEY
fi
if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN_FILE:-}" ] && [ -f "$GITHUB_PERSONAL_ACCESS_TOKEN_FILE" ]; then
    GITHUB_PERSONAL_ACCESS_TOKEN="$(cat "$GITHUB_PERSONAL_ACCESS_TOKEN_FILE")"
    export GITHUB_PERSONAL_ACCESS_TOKEN
fi

# Ensure config exists with default
mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "duckduckgo,n8n,tavily,comfyui,orchestration" > "$CONFIG_FILE"
fi

read_servers() {
  content=$(tr -d '\r\n' < "$CONFIG_FILE" 2>/dev/null | tr ',' '\n' | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//')
  if [ -z "$content" ] && [ -f "$REGISTRY_FILE" ] && command -v jq >/dev/null 2>&1; then
    content=$(jq -r '.servers | keys | join(",")' "$REGISTRY_FILE" 2>/dev/null)
  fi
  printf '%s' "${content:-duckduckgo,n8n,tavily,comfyui,orchestration}"
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
        -e "s|PLACEHOLDER_TAVILY_API_KEY|${TAVILY_API_KEY:-}|g" \
        -e "s|PLACEHOLDER_DASHBOARD_AUTH_TOKEN|${DASHBOARD_AUTH_TOKEN:-}|g" \
        -e "s|PLACEHOLDER_COMFY_MCP_DEFAULT_MODEL|${COMFY_MCP_DEFAULT_MODEL:-flux1-schnell-fp8.safetensors}|g" "$src" >"$dst"
  else
    cp "$src" "$dst"
  fi
}

config_fingerprint() {
  reg_dir="$(dirname "$CONFIG_FILE")"
  custom_src="$reg_dir/registry-custom.yaml"
  custom_dst="$reg_dir/registry-custom.docker.yaml"
  servers="$(read_servers)"
  resolve_registry_custom
  src_sum=""
  dst_sum=""
  if [ -f "$custom_src" ]; then
    src_sum="$(cksum "$custom_src" 2>/dev/null | awk '{print $1":"$2}')"
  fi
  if [ -f "$custom_dst" ]; then
    dst_sum="$(cksum "$custom_dst" 2>/dev/null | awk '{print $1":"$2}')"
  fi
  printf '%s|%s|%s' "$servers" "$src_sum" "$dst_sum"
}

graceful_restart() {
  old_pid="$1"
  kill "$old_pid" 2>/dev/null || true
  i=1
  while [ "$i" -le 30 ]; do
    if ! kill -0 "$old_pid" 2>/dev/null; then
      wait "$old_pid" 2>/dev/null || true
      return 0
    fi
    sleep 1
    i=$((i+1))
  done
  kill -9 "$old_pid" 2>/dev/null || true
  wait "$old_pid" 2>/dev/null || true
}

start_gateway() {
  servers=$(read_servers)
  servers=${servers:-duckduckgo,n8n,tavily,comfyui,orchestration}
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
last_fingerprint=$(config_fingerprint)
pending_fingerprint=""
pending_since=0

while true; do
  sleep "$POLL_SEC"
  current_fingerprint=$(config_fingerprint)
  if [ "$current_fingerprint" != "$last_fingerprint" ]; then
    now=$(date +%s)
    if [ "$pending_fingerprint" != "$current_fingerprint" ]; then
      pending_fingerprint="$current_fingerprint"
      pending_since="$now"
      echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Config change detected. Waiting ${RELOAD_DEBOUNCE_SEC}s for it to settle..."
    elif [ $((now - pending_since)) -ge "$RELOAD_DEBOUNCE_SEC" ]; then
      echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Config stable. Reloading gateway..."
      graceful_restart "$pid"
      pid=$(start_gateway)
      last_fingerprint="$current_fingerprint"
      pending_fingerprint=""
      pending_since=0
    fi
  else
    pending_fingerprint=""
    pending_since=0
  fi
  # Check if gateway died unexpectedly
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] Gateway exited. Restarting..."
    last_fingerprint=""
    pending_fingerprint=""
    pending_since=0
    pid=$(start_gateway)
  fi
done
