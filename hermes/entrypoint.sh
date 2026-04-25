#!/usr/bin/env bash
# hermes/entrypoint.sh — container startup.
# 1. Seeds $HERMES_HOME/config.yaml with Docker-network endpoints.
# 2. Execs the compose-supplied command (hermes gateway, hermes dashboard, etc).
#
# Idempotent: re-writes only the keys we manage (model.* + mcp_servers.gateway.url).
# Preserves any other operator-set keys (skills, memory providers, Discord behavior).
set -eu

HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
mkdir -p "$HERMES_HOME"
export HERMES_HOME

# Bridge from Docker secrets _FILE pattern to the env var the app expects.
# discord.py / hermes read DISCORD_BOT_TOKEN directly from os.environ; the
# compose file mounts the secret at /run/secrets/discord_token and exports
# DISCORD_BOT_TOKEN_FILE pointing to it. If both are set, the file wins.
if [ -n "${DISCORD_BOT_TOKEN_FILE:-}" ] && [ -f "$DISCORD_BOT_TOKEN_FILE" ]; then
    DISCORD_BOT_TOKEN="$(cat "$DISCORD_BOT_TOKEN_FILE")"
    export DISCORD_BOT_TOKEN
fi

HERMES_BIN=/opt/hermes-agent/.venv/bin/hermes

# Seed model + MCP endpoints to Docker-network DNS. hermes config set is idempotent
# and overwrites stale values (e.g. localhost: from a prior host-mode install).
"$HERMES_BIN" config set model.provider        "custom"                        >/dev/null
"$HERMES_BIN" config set model.base_url        "http://model-gateway:11435/v1" >/dev/null
"$HERMES_BIN" config set model.api_key         "${LITELLM_MASTER_KEY:-local}"  >/dev/null
"$HERMES_BIN" config set model.default         "local-chat"                    >/dev/null
# Context window: single source of truth is LLAMACPP_CTX_SIZE in .env. The
# compose file plumbs it into this container's env; the seed below overwrites
# whatever hermes had cached so a change to .env + `docker compose up -d
# hermes-gateway hermes-dashboard` is enough to update the UI progress bar
# (`0/<N>K`). Falls back to 262144 (256k) if unset — matches the stack default.
"$HERMES_BIN" config set model.context_length  "${LLAMACPP_CTX_SIZE:-262144}"  >/dev/null
# Same ceiling for the auxiliary-compression helper model. Hermes's standard
# /v1/models probe on the LiteLLM proxy doesn't expose max_input_tokens (OpenAI
# spec doesn't include it), so without this explicit override hermes falls
# through to its 128K default for 'custom' providers and warns that the
# compression model is smaller than the main-model compression threshold.
# See agent/model_metadata.py get_model_context_length resolution order #0
# and run_agent.py line ~1605 where auxiliary.compression.context_length is read.
"$HERMES_BIN" config set auxiliary.compression.context_length "${LLAMACPP_CTX_SIZE:-262144}" >/dev/null
"$HERMES_BIN" config set mcp_servers.gateway.url "http://mcp-gateway:8811/mcp" >/dev/null

# Bump timeouts for local model. Hermes's default 180s stale-timeout aborts
# prefill on long contexts (22k+ tokens on a dense local model = many minutes).
# 1800s = 30 min. Safety net only — with --reasoning-format deepseek (set in .env via
# LLAMACPP_EXTRA_ARGS) llama-server streams chunks during thinking and this timeout
# should never fire on healthy turns. If it does fire on real workloads, the model
# server is wedged, not slow.
"$HERMES_BIN" config set providers.custom.stale_timeout_seconds   1800 >/dev/null
"$HERMES_BIN" config set providers.custom.request_timeout_seconds 1800 >/dev/null

# Push-through: seed an opinionated SOUL.md and enable the bundled plugin once.
# Sentinel ensures user toggles via `hermes plugins enable/disable` are respected
# on subsequent starts. See docs/hermes-agent.md and the design spec for details.
SEED_MARK="$HERMES_HOME/.ordo-push-through-seeded"
if [ ! -f "$SEED_MARK" ]; then
  if [ ! -f "$HERMES_HOME/SOUL.md" ] || [ ! -s "$HERMES_HOME/SOUL.md" ]; then
    cp /opt/ordo-seed/SOUL.md "$HERMES_HOME/SOUL.md"
  fi
  "$HERMES_BIN" plugins enable push-through >/dev/null 2>&1 || true
  touch "$SEED_MARK"
fi

exec "$@"
