#!/bin/sh
# MCP gateway healthcheck: verify the gateway is listening AND has tools loaded.
# Uses a full MCP session (initialize → tools/list) since the Streamable HTTP
# transport requires a handshake before accepting method calls.
# Falls back to port check if curl is missing.

PORT="${MCP_GATEWAY_PORT:-8811}"
URL="http://localhost:${PORT}/mcp"

if ! command -v curl >/dev/null 2>&1; then
  nc -z localhost "$PORT"
  exit $?
fi

# Step 1: Initialize an MCP session to get a session ID.
INIT_BODY='{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1.0.0"}},"id":1}'
INIT_RESP=$(curl -s -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "$INIT_BODY" --max-time 5 -D /tmp/hc_headers 2>/dev/null)

if [ $? -ne 0 ]; then
  exit 1
fi

# Extract session ID from the Mcp-Session-Id header.
SESSION_ID=$(grep -i 'mcp-session-id' /tmp/hc_headers 2>/dev/null | tr -d '\r' | sed 's/.*: *//')

# Step 2: Send initialized notification (fire and forget).
if [ -n "$SESSION_ID" ]; then
  NOTIF='{"jsonrpc":"2.0","method":"notifications/initialized"}'
  curl -s -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    -d "$NOTIF" --max-time 3 >/dev/null 2>&1
fi

# Step 3: Request tools/list within the session.
TOOLS_BODY='{"jsonrpc":"2.0","method":"tools/list","id":2}'
SESSION_HEADER=""
[ -n "$SESSION_ID" ] && SESSION_HEADER="-H Mcp-Session-Id:${SESSION_ID}"

# shellcheck disable=SC2086
TOOLS_RESP=$(curl -s -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  $SESSION_HEADER \
  -d "$TOOLS_BODY" --max-time 5 2>/dev/null)

if [ $? -ne 0 ]; then
  exit 1
fi

# Step 4: Terminate session (best-effort cleanup).
if [ -n "$SESSION_ID" ]; then
  curl -s -X DELETE "$URL" \
    -H "Mcp-Session-Id: $SESSION_ID" \
    --max-time 2 >/dev/null 2>&1
fi

# Parse SSE: extract the data line(s) from event stream.
# The gateway returns "event: message\ndata: {...}\n" format.
DATA_LINE=$(printf '%s' "$TOOLS_RESP" | grep '^data: ' | head -1 | sed 's/^data: //')
[ -z "$DATA_LINE" ] && DATA_LINE="$TOOLS_RESP"

# Check that the response contains at least one tool.
if command -v jq >/dev/null 2>&1; then
  COUNT=$(printf '%s' "$DATA_LINE" | jq -r '.result.tools | length' 2>/dev/null)
  if [ "$COUNT" -gt 0 ] 2>/dev/null; then
    exit 0
  fi
  exit 1
fi

# No jq: heuristic check for non-empty tools array.
printf '%s' "$DATA_LINE" | grep -q '"tools":\[.\+\]'
