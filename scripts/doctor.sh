#!/usr/bin/env bash
# Doctor: quick health probes + optional openclaw.json validation (M7).
# Usage: ./scripts/doctor.sh
# Env: MODEL_GATEWAY_URL, MCP_GATEWAY_URL, DASHBOARD_URL, OPENCLAW_CONFIG_PATH, ORDO_AI_STACK_ROOT
#      DOCTOR_DEPS_TIMEOUT_SEC - max seconds for GET /api/dependencies (default 120)
#      DOCTOR_STRICT=1 - optional Ollama/MCP host probes fail hard if unreachable
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MG="${MODEL_GATEWAY_URL:-http://localhost:11435}"
MCP="${MCP_GATEWAY_URL:-http://localhost:8811}"
DASH="${DASHBOARD_URL:-http://localhost:8080}"

# Optional Bearer for dashboard when DASHBOARD_AUTH_TOKEN is set (or read from repo .env)
if [ -z "${DASHBOARD_AUTH_TOKEN:-}" ] && [ -f "$REPO_ROOT/.env" ]; then
  line="$(grep -E '^[[:space:]]*DASHBOARD_AUTH_TOKEN=' "$REPO_ROOT/.env" | head -1 || true)"
  if [ -n "$line" ]; then
    DASHBOARD_AUTH_TOKEN="${line#*=}"
    DASHBOARD_AUTH_TOKEN="${DASHBOARD_AUTH_TOKEN%%$'\r'}"
    DASHBOARD_AUTH_TOKEN="${DASHBOARD_AUTH_TOKEN#\"}"
    DASHBOARD_AUTH_TOKEN="${DASHBOARD_AUTH_TOKEN%\"}"
    DASHBOARD_AUTH_TOKEN="${DASHBOARD_AUTH_TOKEN#\'}"
    DASHBOARD_AUTH_TOKEN="${DASHBOARD_AUTH_TOKEN%\'}"
    export DASHBOARD_AUTH_TOKEN
  fi
fi

DASH_CURL_AUTH=()
if [ -n "${DASHBOARD_AUTH_TOKEN:-}" ]; then
  DASH_CURL_AUTH=(-H "Authorization: Bearer ${DASHBOARD_AUTH_TOKEN}")
fi

FAIL=0

probe() {
  local name="$1"
  local url="$2"
  local extra=()
  case "$url" in
    "$DASH"/*) extra=("${DASH_CURL_AUTH[@]}"); ;;
  esac
  if curl -sf --max-time 5 "${extra[@]}" "$url" > /dev/null 2>&1; then
    echo "  OK   $name"
  else
    echo "  FAIL $name ($url)" >&2
    FAIL=1
  fi
}

# Aggregates many HTTP probes; needs a long client timeout (default 5s is too short).
probe_dependencies() {
  local name="$1"
  local url="$2"
  local maxt="${DOCTOR_DEPS_TIMEOUT_SEC:-120}"
  local extra=()
  case "$url" in
    "$DASH"/*) extra=("${DASH_CURL_AUTH[@]}"); ;;
  esac
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time "$maxt" "${extra[@]}" "$url" 2>/dev/null || echo "000")"
  if [ "$code" = "200" ]; then
    echo "  OK   $name"
  elif [ "$code" = "404" ]; then
    echo "  WARN $name - not found (HTTP 404); rebuild: docker compose build dashboard"
  else
    echo "  FAIL $name ($url) (HTTP ${code})" >&2
    FAIL=1
  fi
}

# GET /ready returns 503 when not ready — still a successful connection to the gateway.
probe_ready() {
  local name="$1"
  local url="$2"
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")"
  if [ "$code" = "200" ]; then
    echo "  OK   $name"
  elif [ "$code" = "503" ]; then
    echo "  WARN $name - not ready (HTTP 503); pull a model or fix backends"
  elif [ "$code" = "404" ]; then
    echo "  WARN $name - GET /ready not found (HTTP 404); rebuild: docker compose build model-gateway"
  else
    echo "  FAIL $name ($url) (HTTP ${code})" >&2
    FAIL=1
  fi
}

probe_optional_backend_host() {
  local name="$1"
  local url="$2"
  local hint="$3"
  if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
    echo "  OK   $name"
  elif [ "${DOCTOR_STRICT:-}" = "1" ]; then
    echo "  FAIL $name ($url)" >&2
    FAIL=1
  else
    echo "  WARN $name - not reachable on host ($url). Default compose keeps this backend internal. $hint"
  fi
}

# GET /mcp may return 4xx without a proper Streamable HTTP body; any HTTP status means the port is up.
probe_mcp_gateway_optional() {
  local name="$1"
  local url="$2"
  local hint="$3"
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>/dev/null || echo "000")"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    echo "  OK   $name (HTTP $code)"
  elif [ "${DOCTOR_STRICT:-}" = "1" ]; then
    echo "  FAIL $name ($url)" >&2
    FAIL=1
  else
    echo "  WARN $name - not reachable on host ($url). Default compose keeps this backend internal. $hint"
  fi
}

echo "==> Ordo AI Stack doctor (M7)"
echo "==> Probes (published host ports)"
probe "dashboard /api/health"      "$DASH/api/health"
probe_dependencies "dashboard /api/dependencies" "$DASH/api/dependencies"
probe "model-gateway /health"      "$MG/health"
probe_ready "model-gateway /ready"       "$MG/ready"
echo "==> Probes (optional: MCP on localhost only if you use mcp-expose override)"
probe_mcp_gateway_optional "mcp-gateway /mcp" "$MCP/mcp" "See overrides/mcp-expose.yml"

echo "==> OpenClaw config validation"
if python3 "$SCRIPT_DIR/validate_openclaw_config.py"; then
  :
else
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo "==> doctor FAILED"
  exit 1
fi
echo "==> doctor PASSED"
exit 0
