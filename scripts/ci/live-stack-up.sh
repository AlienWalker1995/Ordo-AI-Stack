#!/usr/bin/env bash
# Bring up the smallest real stack the live docker tests check, on a machine with NO other stack.
#
# Run by CI's `docker` job (.github/workflows/ci.yml) on a fresh runner, from the repo root. It
# brings up compose project `ordo` with the rendered network names (`ordo-net`) and host binds, so
# NEVER run it on a host that runs the real stack: use a throwaway daemon (a CI runner, or a
# docker:dind container).
#
# What comes up, and why each is needed by tests/test_secrets_isolation.py and
# tests/test_hermes_docker_access.py:
#   agent            the Hermes gateway: docker socket, root group, file secrets, ops-controller env
#   ops-controller   the agent must reach it at http://ops-controller:9000/health
#   caddy            the netns owner hermes-dashboard lives in (`ordo up caddy` names its members)
#   hermes-dashboard must have neither the socket nor the root group
# Everything goes through the operator's own commands (`ordo init`, `ordo remote enable`,
# `ordo up`), so the containers are the ones a fresh install renders. The edge needs Google OAuth
# values and a TLS certificate to start; both are placeholders here, since nothing signs in.
set -euo pipefail

OUT=out
PROJECT=ordo
WAIT_SECONDS=${WAIT_SECONDS:-180}

if docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '{{.Names}}' | grep -q .; then
  echo "refusing: compose project '${PROJECT}' already has containers on this daemon" >&2
  exit 1
fi

python -m ordo init --yes --out "$OUT"
OAUTH2_PROXY_CLIENT_SECRET=ci-placeholder python -m ordo remote enable --yes --out "$OUT" \
  --hostname ordo.ci.test --bind 127.0.0.1 --client-id ci-placeholder --emails ci@example.test

mkdir -p auth/caddy/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=ordo.ci.test" \
  -keyout auth/caddy/certs/tailnet.key -out auth/caddy/certs/tailnet.crt 2>/dev/null

# --no-preflight: the host checks (GPU runtime, disk, ports) are about an operator's machine.
# --no-fetch: none of these services loads a model.
python -m ordo up --no-preflight --no-fetch --out "$OUT" ops-controller agent caddy

containers=("${PROJECT}-ops-controller-1" "${PROJECT}-agent-1" "${PROJECT}-caddy-1" "${PROJECT}-hermes-dashboard-1")
deadline=$((SECONDS + WAIT_SECONDS))
while :; do
  not_running=()
  for name in "${containers[@]}"; do
    running=$(docker inspect -f '{{.State.Running}} {{.State.Restarting}}' "$name" 2>/dev/null || echo "missing")
    [ "$running" = "true false" ] || not_running+=("$name")
  done
  # Health, not just "running": the gateway writes gateway_state.json once it is serving.
  agent_health=$(docker inspect -f '{{.State.Health.Status}}' "${PROJECT}-agent-1" 2>/dev/null || echo "missing")
  if [ ${#not_running[@]} -eq 0 ] && [ "$agent_health" = "healthy" ]; then
    echo "live stack up: ${containers[*]} (agent ${agent_health})"
    exit 0
  fi
  if [ $SECONDS -ge $deadline ]; then
    echo "live stack not up after ${WAIT_SECONDS}s: not running: ${not_running[*]:-none}; agent health: ${agent_health}" >&2
    for name in "${containers[@]}"; do
      echo "--- $name" >&2
      docker logs --tail 40 "$name" >&2 2>&1 || true
    done
    exit 1
  fi
  sleep 5
done
