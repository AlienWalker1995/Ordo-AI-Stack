#!/bin/sh
# health_check.sh — run from inside openclaw container to check all stack services
# Usage: sh /home/node/.openclaw/workspace/health_check.sh
# Requires: DASHBOARD_URL and DASHBOARD_AUTH_TOKEN env vars

set -e

DASHBOARD_URL="${DASHBOARD_URL:-http://dashboard:8080}"
AUTH="${DASHBOARD_AUTH_TOKEN:-}"

if [ -z "$AUTH" ]; then
  echo "WARNING: DASHBOARD_AUTH_TOKEN not set — health check may return 401"
fi

echo "=== Stack Health ==="
HEALTH=$(wget -q -O - --header="Authorization: Bearer $AUTH" "$DASHBOARD_URL/api/health" 2>&1) || {
  echo "FAIL: Could not reach dashboard at $DASHBOARD_URL"
  exit 1
}
echo "$HEALTH" | grep -o '"[^"]*":"[^"]*"' | grep -E '"status"' | head -20
echo ""

echo "=== Unhealthy Services ==="
echo "$HEALTH" | grep -v '"healthy"' | grep '"status"' | head -10 || echo "(none)"
echo ""

echo "=== ComfyUI Models ==="
wget -q -O - "http://comfyui:8188/models/checkpoints" 2>/dev/null | head -5 || echo "ComfyUI unreachable or no checkpoints endpoint"
echo ""

echo "=== Active MCP Servers ==="
cat /home/node/.openclaw/workspace/data/mcp/servers.txt 2>/dev/null || echo "(servers.txt not found)"
echo ""

echo "=== Ollama Running Models ==="
wget -q -O - "http://model-gateway:11435/api/ps" 2>/dev/null | grep -o '"name":"[^"]*"' | head -5 || echo "No models loaded"
echo ""

echo "=== Done ==="
