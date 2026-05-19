#!/usr/bin/env bash
# hermes/entrypoint.sh — container startup.
# 1. As root: ensure $HERMES_HOME is writable by the unprivileged hermes user
#    (bind mounts from the host can land with mismatched ownership on Docker
#    Desktop / virtiofs; without a chmod here, hermes crash-loops on
#    `os.mkdir(/home/hermes/.hermes/cron): EACCES`).
# 2. As hermes (via gosu): seed $HERMES_HOME/config.yaml with Docker-network
#    endpoints and exec the compose-supplied command.
#
# Mirror of dashboard/entrypoint.sh's gosu pattern.
# Idempotent: re-writes only the keys we manage (model.* + mcp_servers.gateway.url).
# Preserves any other operator-set keys (skills, memory providers, Discord behavior).
set -eu

HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"

# Run as root: ensure the bind-mounted home is writable by the hermes user.
mkdir -p "$HERMES_HOME"
if ! gosu hermes sh -c "test -w '$HERMES_HOME'" 2>/dev/null; then
    chmod -R a+rwX "$HERMES_HOME" 2>/dev/null || true
fi
export HERMES_HOME

# Same ownership/writability check for the gameplay manifest used by the
# social-relay reel pipeline. ComfyUI writes outputs into this directory as
# root, which leaves the manifest unwritable for the unprivileged hermes user
# the agent's execute_code sandbox runs as (uid 1000). Without this, the cron
# completes its run but cannot record the gameplay segment in `used`, so the
# next run can pick the same segment again. Idempotent.
COMFYUI_OUTPUT_DIR=/workspace/data/comfyui-output
GAMEPLAY_MANIFEST="$COMFYUI_OUTPUT_DIR/gameplay_manifest.json"
if [ -d "$COMFYUI_OUTPUT_DIR" ] && ! gosu hermes sh -c "test -w '$COMFYUI_OUTPUT_DIR'" 2>/dev/null; then
    chmod 775 "$COMFYUI_OUTPUT_DIR" 2>/dev/null || true
fi
if [ -f "$GAMEPLAY_MANIFEST" ] && ! gosu hermes sh -c "test -w '$GAMEPLAY_MANIFEST'" 2>/dev/null; then
    chown hermes:hermes "$GAMEPLAY_MANIFEST" 2>/dev/null || true
    chmod 664 "$GAMEPLAY_MANIFEST" 2>/dev/null || true
fi

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
gosu hermes "$HERMES_BIN" config set model.provider        "custom"                        >/dev/null
gosu hermes "$HERMES_BIN" config set model.base_url        "http://model-gateway:11435/v1" >/dev/null
gosu hermes "$HERMES_BIN" config set model.api_key         "${LITELLM_MASTER_KEY:-local}"  >/dev/null
gosu hermes "$HERMES_BIN" config set model.default         "local-chat"                    >/dev/null
# Context window: single source of truth is LLAMACPP_CTX_SIZE in .env. The
# compose file plumbs it into this container's env; the seed below overwrites
# whatever hermes had cached so a change to .env + `docker compose up -d
# hermes-gateway hermes-dashboard` is enough to update the UI progress bar
# (`0/<N>K`). Falls back to 262144 (256k) if unset — matches the stack default.
gosu hermes "$HERMES_BIN" config set model.context_length  "${LLAMACPP_CTX_SIZE:-262144}"  >/dev/null
# Per-turn budgets — hoisted from in-container config.yaml so they're
# monitorable from .env. See the matching env vars in docker-compose.yml's
# hermes-gateway / hermes-dashboard service blocks.
# - model.max_tokens: output cap per LLM call. Without this Hermes computes a
#   smaller default that truncates tool-heavy turns and triggers a 3-retry
#   continuation loop that often still fails. Match LLAMACPP_N_PREDICT.
# - agent.max_turns: tool-use iteration ceiling per Hermes turn.
# - agent.gateway_timeout: wall-clock cap on a single turn (distinct from
#   the stream-stale detector, which is HERMES_STREAM_STALE_TIMEOUT below).
gosu hermes "$HERMES_BIN" config set model.max_tokens       "${HERMES_MAX_TOKENS:-65536}"      >/dev/null
gosu hermes "$HERMES_BIN" config set agent.max_turns        "${HERMES_MAX_TURNS:-90}"          >/dev/null
gosu hermes "$HERMES_BIN" config set agent.gateway_timeout  "${HERMES_GATEWAY_TIMEOUT:-3600}"  >/dev/null
gosu hermes "$HERMES_BIN" config set agent.api_max_retries  "${HERMES_API_MAX_RETRIES:-10}"    >/dev/null
# Same ceiling for the auxiliary-compression helper model. Hermes's standard
# /v1/models probe on the LiteLLM proxy doesn't expose max_input_tokens (OpenAI
# spec doesn't include it), so without this explicit override hermes falls
# through to its 128K default for 'custom' providers and warns that the
# compression model is smaller than the main-model compression threshold.
# See agent/model_metadata.py get_model_context_length resolution order #0
# and run_agent.py line ~1605 where auxiliary.compression.context_length is read.
gosu hermes "$HERMES_BIN" config set auxiliary.compression.context_length "${LLAMACPP_CTX_SIZE:-262144}" >/dev/null
gosu hermes "$HERMES_BIN" config set mcp_servers.gateway.url "http://mcp-gateway:8811/mcp" >/dev/null

# Bump timeouts for local model. Hermes's default 180s stale-timeout aborts
# prefill on long contexts (22k+ tokens on a dense local model = many minutes).
# 1800s = 30 min. Safety net only — with --reasoning-format deepseek (set in .env via
# LLAMACPP_EXTRA_ARGS) llama-server streams chunks during thinking and this timeout
# should never fire on healthy turns. If it does fire on real workloads, the model
# server is wedged, not slow.
gosu hermes "$HERMES_BIN" config set providers.custom.stale_timeout_seconds   1800 >/dev/null
gosu hermes "$HERMES_BIN" config set providers.custom.request_timeout_seconds 1800 >/dev/null

# Push-through: seed an opinionated SOUL.md and enable the bundled plugin once.
# Sentinel ensures user toggles via `hermes plugins enable/disable` are respected
# on subsequent starts. See docs/hermes-agent.md and the design spec for details.
SEED_MARK="$HERMES_HOME/.ordo-push-through-seeded"
if [ ! -f "$SEED_MARK" ]; then
  if [ ! -f "$HERMES_HOME/SOUL.md" ] || [ ! -s "$HERMES_HOME/SOUL.md" ]; then
    cp /opt/ordo-seed/SOUL.md "$HERMES_HOME/SOUL.md"
    chown hermes:hermes "$HERMES_HOME/SOUL.md" 2>/dev/null || true
  fi
  gosu hermes "$HERMES_BIN" plugins enable push-through >/dev/null 2>&1 || true
  gosu hermes "$HERMES_BIN" plugins enable ops-router    >/dev/null 2>&1 || true
  gosu hermes touch "$SEED_MARK"
fi

# Drop privileges and exec the compose-supplied command (hermes gateway / dashboard / etc).
exec gosu hermes "$@"
