#!/usr/bin/env bash
# services/hermes/entrypoint.sh — container startup.
# 1. As root: ensure $HERMES_HOME is writable by the unprivileged hermes user
#    (bind mounts from the host can land with mismatched ownership on Docker
#    Desktop / virtiofs; without a chmod here, hermes crash-loops on
#    `os.mkdir(/home/hermes/.hermes/cron): EACCES`).
# 2. As hermes (via gosu): seed $HERMES_HOME/config.yaml with Docker-network
#    endpoints and exec the compose-supplied command.
#
# Mirror of dashboard/entrypoint.sh's gosu pattern.
# Idempotent: re-writes only the keys we manage (model.* + mcp_servers.gateway.*).
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
# render output pipeline. ComfyUI writes outputs into this directory as
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

# Same bridge for the backup-repo PAT: SOPS Docker secret at
# /run/secrets/github_backup_pat -> GITHUB_BACKUP_PAT env var that Hermes and
# git expect. Secrets live in SOPS, never in .env; this is how they reach the env.
if [ -n "${GITHUB_BACKUP_PAT_FILE:-}" ] && [ -f "$GITHUB_BACKUP_PAT_FILE" ]; then
    GITHUB_BACKUP_PAT="$(cat "$GITHUB_BACKUP_PAT_FILE")"
    export GITHUB_BACKUP_PAT
fi

HERMES_BIN=/opt/hermes-agent/.venv/bin/hermes

# Hermes API server gate (the endpoint the `evals` runner drives at http://agent:8642/v1).
# Hermes enables the api_server platform from ENV alone (API_SERVER_KEY/HOST/PORT, see
# gateway/config.py::_apply_env_overrides), so this is an env gate rather than a `config set`
# seed: seeding platforms.api_server into config.yaml would put the key on disk in the brain
# volume. Enabled ONLY with a non-empty key of at least 16 characters (Hermes's own startup guard;
# the wizard mints token_urlsafe(32)). Anything else unsets all three so the gateway never
# enrolls the platform: an empty key means the evals plugin is off, and a short key would only
# make the adapter log a fatal "api_server_key_invalid" and leave the platform dead. A bad key must
# never take the agent down, so it is a loud warning, not an exit.
if [ -n "${API_SERVER_KEY:-}" ] && [ "${#API_SERVER_KEY}" -ge 16 ]; then
    echo "entrypoint: Hermes API server enabled on ${API_SERVER_HOST:-127.0.0.1}:${API_SERVER_PORT:-8642} (internal network only)" >&2
else
    if [ -n "${API_SERVER_KEY:-}" ]; then
        echo "entrypoint: WARNING HERMES_API_SERVER_KEY is shorter than 16 characters; Hermes API server left DISABLED" >&2
    fi
    unset API_SERVER_KEY API_SERVER_HOST API_SERVER_PORT
fi

# Fail loud: LITELLM_KEY_HERMES is Hermes' own LiteLLM virtual key (chat + embeddings + MCP tools),
# provisioned by the model-gateway-keys one-shot from secrets.env. Refuse to start without it.
: "${LITELLM_KEY_HERMES:?LITELLM_KEY_HERMES must be set (SOPS/secrets.env) - refusing to seed a guessable default}"

# Seed model + MCP endpoints to Docker-network DNS. hermes config set is idempotent
# and overwrites stale values (e.g. localhost: from a prior host-mode install).
gosu hermes "$HERMES_BIN" config set model.provider        "custom"                        >/dev/null
gosu hermes "$HERMES_BIN" config set model.base_url        "http://model-gateway:11435/v1" >/dev/null
gosu hermes "$HERMES_BIN" config set model.api_key         "${LITELLM_KEY_HERMES}"         >/dev/null
gosu hermes "$HERMES_BIN" config set model.default         "local-chat"                    >/dev/null
# Context window: single source of truth is LLAMACPP_CTX_SIZE in .env. The
# compose file plumbs it into this container's env; the seed below overwrites
# whatever hermes had cached so a re-render + `ordo recreate agent
# hermes-dashboard` is enough to update the UI progress bar
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
# Compaction trigger as a fraction of the effective input budget (context_length - max_tokens).
# Only seed when explicitly set so this stays a no-op (framework default) for deployments that
# don't tune it. With a large max_tokens the default floors the trigger at MINIMUM_CONTEXT_LENGTH
# (64K); raising this fraction compacts later. The consumed config key is `compression.threshold`
# (agent_init.py reads _compression_cfg.get("threshold") and passes it as the compressor's
# threshold_percent) — NOT `compression.threshold_percent`, which the app ignores. Env var keeps
# the _PERCENT name because the VALUE is a 0-1 fraction. See agent/context_compressor.py.
if [ -n "${HERMES_COMPRESSION_THRESHOLD_PERCENT:-}" ]; then
  gosu hermes "$HERMES_BIN" config set compression.threshold "${HERMES_COMPRESSION_THRESHOLD_PERCENT}" >/dev/null
fi
# MCP tools come from LiteLLM's MCP gateway on the model-gateway (aggregates every mcp-* service the
# key is granted). Authenticated with the same virtual key; tools arrive as <server_id>-<tool>.
gosu hermes "$HERMES_BIN" config set mcp_servers.gateway.url "http://model-gateway:11435/mcp" >/dev/null
gosu hermes "$HERMES_BIN" config set mcp_servers.gateway.headers.Authorization "Bearer ${LITELLM_KEY_HERMES}" >/dev/null

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

# Langfuse tracing: enable the BUNDLED observability/langfuse plugin once, and only when the
# langfuse stack actually issued us keys. Its OWN sentinel (not the push-through one) because
# the two are enabled under different conditions and at different times: a stack that adds
# langfuse later has long since passed the push-through sentinel, and sharing it would mean the
# plugin never gets enabled at all.
#
# The HERMES_LANGFUSE_PUBLIC_KEY guard is what keeps this fail-open. With the langfuse plugin
# disabled the compose ref interpolates to "", so we skip entirely and no sentinel is written -
# the next start after the operator enables langfuse picks it up. Enabling it keyless would
# leave an "enabled" plugin whose hooks are permanently inert, which reads as working tracing.
#
# Idempotent + operator-respecting, exactly like the block above: once the sentinel exists a
# later `hermes plugins disable observability/langfuse` is never undone on restart.
LANGFUSE_MARK="$HERMES_HOME/.ordo-langfuse-seeded"
if [ -n "${HERMES_LANGFUSE_PUBLIC_KEY:-}" ] && [ ! -f "$LANGFUSE_MARK" ]; then
  gosu hermes "$HERMES_BIN" plugins enable observability/langfuse >/dev/null 2>&1 || true
  gosu hermes touch "$LANGFUSE_MARK"
fi

# Repo-owned skills ship read-only in the image at /opt/ordo-skills (see the Dockerfile). Register
# that dir as a Hermes external skills dir and move aside any local skill of the same name, which
# would otherwise hide the shipped one. Every start, idempotent, and it never blocks boot. Runs
# before the config.yaml permission tightening below because it may write config.yaml.
timeout 60 gosu hermes python3 /opt/ordo/ordo_skills.py boot || true

# config.yaml holds provider API keys. The writable-home repair above (chmod -R a+rwX)
# and volume-migration copies can leave it world-readable (found live at 0777 on
# 2026-08-07 despite save_config_value's own 0600 chmod), so tighten it explicitly on
# every start — after all seeding writes, right before the gateway runs. Idempotent.
if [ -f "$HERMES_HOME/config.yaml" ]; then
    chown hermes:hermes "$HERMES_HOME/config.yaml" 2>/dev/null || true
    chmod 600 "$HERMES_HOME/config.yaml" 2>/dev/null || true
fi

# Federate memory files with the Obsidian vault before the gateway reads them.
# Operator (vault) edits win; must never block boot (script always exits 0).
# Run as hermes (gosu), not root: it writes under $HERMES_HOME (SOUL.md,
# memories/, state/), and root-owned output there breaks later hermes writes
# the same way root cron registration bricks jobs.json (see entrypoint header).
# Idempotent ownership repair: root-owned memory files brick hermes-user writes
# and silently kill federation of the MEMORY pair.
chown hermes:hermes "$HERMES_HOME/SOUL.md" "$HERMES_HOME/memories/"*.md 2>/dev/null || true
# 9p reads can wedge; boot must have a ceiling.
timeout 60 gosu hermes python3 /opt/ordo/vault_federate.py || true

# Drop privileges and exec the compose-supplied command (hermes gateway / dashboard / etc).
exec gosu hermes "$@"
