#!/usr/bin/env bash
# Smoke test: bring up services and verify health.
# Usage: ./scripts/smoke_test.sh [--no-up]  (default: runs docker compose up -d first)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

NO_UP=false
for arg in "$@"; do
  case "$arg" in
    --no-up) NO_UP=true ;;
  esac
done

echo "==> Smoke test (repo: $REPO_ROOT)"

if [ "$NO_UP" = false ]; then
  echo "==> Starting services..."
  docker compose up -d
  echo "==> Waiting 60s for healthchecks..."
  sleep 60
fi

FAIL=0

check() {
  local name="$1"
  local url="$2"
  local expected="${3:-}"
  if curl -sf "$url" > /dev/null 2>&1; then
    echo "  OK $name"
  else
    echo "  FAIL $name ($url)"
    FAIL=1
  fi
}

echo "==> Checking health endpoints..."
check "dashboard"      "http://localhost:8080/api/health"
check "model-gateway"  "http://localhost:11435/health"
check "mcp-gateway"    "http://localhost:8811/mcp"

echo "==> Service status"
docker compose ps

if [ $FAIL -eq 1 ]; then
  echo "==> Smoke test FAILED"
  exit 1
fi

echo "==> Smoke test PASSED"
exit 0
