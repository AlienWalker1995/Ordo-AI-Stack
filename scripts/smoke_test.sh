#!/usr/bin/env bash
# Smoke test: verify the running stack's health. Changes nothing unless asked.
# Usage: ./scripts/smoke_test.sh [--up]  (--up first brings the stack up the canonical way)
#
# Targets the rendered compose (out/docker-compose.yml, project "ordo"). Only Caddy
# publishes a host port (:443) — every other service is ordo-net-internal, so health is
# probed with `docker compose exec` against the same in-container commands each service's
# own healthcheck already uses, not host-port curls.
set -e

# On Windows Git Bash, MSYS mangles absolute container paths (e.g. /mcp-scripts/...) passed
# through to docker.exe into host paths before they reach the container. No-op elsewhere.
export MSYS_NO_PATHCONV=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Both env files, always: compose interpolates ${...} from them, and a call without secrets.env
# renders blank secrets, so an `up` would recreate services with empty credentials.
COMPOSE_ARGS=(--project-directory out -f out/docker-compose.yml -p ordo
              --env-file out/.env --env-file out/secrets.env --env-file out/secret-files.env)

UP=false
for arg in "$@"; do
  case "$arg" in
    --up) UP=true ;;
    --no-up) ;;  # the default now; accepted so old invocations keep working
  esac
done

echo "==> Smoke test (repo: $REPO_ROOT, compose: out/docker-compose.yml, project: ordo)"

if [ "$UP" = true ]; then
  echo "==> Starting services..."
  # The sanctioned bring-up: every profile, both env files, refused while a GPU lease holds.
  python -m ordo up --all --out out
  echo "==> Waiting 60s for healthchecks..."
  sleep 60
fi

FAIL=0

# Probes in-network via `docker compose exec`, reusing each service's own healthcheck
# command (see out/docker-compose.yml) instead of curling unpublished host ports.
check_exec() {
  local name="$1"
  local service="$2"
  shift 2
  if docker compose "${COMPOSE_ARGS[@]}" exec -T "$service" "$@" > /dev/null 2>&1; then
    echo "  OK $name"
  else
    echo "  FAIL $name (exec in $service)"
    FAIL=1
  fi
}

echo "==> Checking health endpoints (in-network)..."
# model-gateway's master key is a file under /run/secrets (LITELLM_MASTER_KEY_FILE): an exec'd probe
# does not see the entrypoint's environment, so it reads the file, as the service healthcheck does.
check_exec "dashboard" dashboard python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"
check_exec "model-gateway" model-gateway python3 -c \
  "import os, urllib.request; req = urllib.request.Request('http://localhost:11435/v1/models', headers={'Authorization': 'Bearer ' + open(os.environ['LITELLM_MASTER_KEY_FILE']).read().strip()}); urllib.request.urlopen(req)"
# MCP is served by model-gateway (LiteLLM /mcp); its server list must not be empty.
check_exec "mcp (model-gateway)" model-gateway python3 -c \
  "import json, os, urllib.request; req = urllib.request.Request('http://localhost:11435/v1/mcp/server', headers={'Authorization': 'Bearer ' + open(os.environ['LITELLM_MASTER_KEY_FILE']).read().strip()}); assert json.load(urllib.request.urlopen(req)), 'no MCP servers registered'"

echo "==> Service status"
docker compose "${COMPOSE_ARGS[@]}" ps

if [ $FAIL -eq 1 ]; then
  echo "==> Smoke test FAILED"
  exit 1
fi

echo "==> Smoke test PASSED"
exit 0
