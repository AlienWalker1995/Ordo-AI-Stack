#!/bin/sh
# health_check.sh — run from inside openclaw container to check all stack services
# Usage: sh /home/node/.openclaw/workspace/health_check.sh
# Requires: DASHBOARD_URL; optional DASHBOARD_AUTH_TOKEN if dashboard auth is enabled

set -e

DASHBOARD_URL="${DASHBOARD_URL:-http://dashboard:8080}"
AUTH="${DASHBOARD_AUTH_TOKEN:-}"

if [ -z "$AUTH" ]; then
  echo "NOTE: DASHBOARD_AUTH_TOKEN not set — using unauthenticated /api/health (OK if dashboard has no Bearer requirement)"
fi

echo "=== Stack Health (GET /api/health) ==="
if [ -n "$AUTH" ]; then
  HEALTH=$(wget -q -O - --header="Authorization: Bearer $AUTH" "$DASHBOARD_URL/api/health" 2>&1) || {
    echo "FAIL: Could not reach dashboard at $DASHBOARD_URL"
    exit 1
  }
else
  HEALTH=$(wget -q -O - "$DASHBOARD_URL/api/health" 2>&1) || {
    echo "FAIL: Could not reach dashboard at $DASHBOARD_URL"
    exit 1
  }
fi
echo "$HEALTH"
echo ""

if echo "$HEALTH" | grep -q '"ok":false'; then
  echo "WARNING: At least one service reported ok:false (see JSON above)."
else
  echo "All reported service checks are ok:true."
fi
echo ""

echo "=== ComfyUI Models ==="
wget -q -O - "http://comfyui:8188/models/checkpoints" 2>/dev/null | head -5 || echo "ComfyUI unreachable or no checkpoints endpoint"
echo ""

echo "=== Active MCP Servers ==="
cat /home/node/.openclaw/workspace/data/mcp/servers.txt 2>/dev/null || echo "(servers.txt not found)"
echo ""

echo "=== Gateway Models ==="
wget -q -O - "http://model-gateway:11435/v1/models" 2>/dev/null | grep -o '"id":"[^"]*"' | head -5 || echo "No models loaded"
echo ""

echo "=== Done ==="
