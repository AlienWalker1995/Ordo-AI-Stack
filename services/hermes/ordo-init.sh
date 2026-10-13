#!/command/with-contenv sh
# shellcheck shell=sh
# Ordo stack boot hook for the Hermes image (installed as /etc/cont-init.d/50-ordo).
#
# s6-overlay runs this as root after upstream's 01-hermes-setup (UID remap, brain-volume ownership,
# config seeding) and before the CMD (`hermes gateway` / `hermes dashboard`). It applies this
# stack's configuration through Hermes's own CLI and touches nothing Hermes ships. The image sets
# S6_BEHAVIOUR_IF_STAGE2_FAILS=2, so a failure here stops the container instead of starting an
# unconfigured gateway.
#
# s6 starts the CMD with a scrubbed environment rebuilt from /run/s6/container_environment, so a
# value this hook derives for the gateway is published there (publish_env), the same mechanism
# upstream's hook uses; a plain `export` here would not reach it.
set -eu

HERMES_HOME="${HERMES_HOME:-/opt/data}"
HERMES_BIN=/opt/hermes/.venv/bin/hermes
HERMES_PYTHON=/opt/hermes/.venv/bin/python
CONTAINER_ENV=/run/s6/container_environment

as_hermes() { /command/s6-setuidgid hermes "$@"; }
hermes_config() { as_hermes "$HERMES_BIN" config set "$1" "$2" >/dev/null; }
# Remove a key if present (`config unset` fails on an absent key).
hermes_config_unset() {
    if as_hermes "$HERMES_BIN" config get "$1" >/dev/null 2>&1; then
        as_hermes "$HERMES_BIN" config unset "$1" >/dev/null
    fi
}
publish_env() { mkdir -p "$CONTAINER_ENV" && printf '%s' "$2" > "$CONTAINER_ENV/$1"; }

# File-delivered secrets (ordo/render/secret_files.py): Hermes reads these from its environment
# and has no *_FILE option, so the canonical reader resolves each one the container declares and
# the value is published to the CMD's environment. DISCORD_BOT_TOKEN (the agent's gateway),
# GITHUB_BACKUP_PAT (the backup scripts; git itself reads the file through
# git-credential-github-pat), HERMES_DASHBOARD_OIDC_CLIENT_SECRET (the dashboard's Google login).
. /opt/ordo/secret-env.sh
ordo_secret_file_env DISCORD_BOT_TOKEN GITHUB_BACKUP_PAT HERMES_DASHBOARD_OIDC_CLIENT_SECRET
[ -z "${DISCORD_BOT_TOKEN:-}" ] || publish_env DISCORD_BOT_TOKEN "$DISCORD_BOT_TOKEN"
[ -z "${GITHUB_BACKUP_PAT:-}" ] || publish_env GITHUB_BACKUP_PAT "$GITHUB_BACKUP_PAT"
[ -z "${HERMES_DASHBOARD_OIDC_CLIENT_SECRET:-}" ] || publish_env HERMES_DASHBOARD_OIDC_CLIENT_SECRET "$HERMES_DASHBOARD_OIDC_CLIENT_SECRET"

# Hermes API server. Upstream always runs it (cron fires go through it) and, when the container
# brings no key, generates one into $HERMES_HOME/.env. The stack exposes it on the project network
# (API_SERVER_HOST=0.0.0.0, for the evals runner) only when HERMES_API_SERVER_KEY is set; otherwise
# it stays on loopback, upstream's default.
if [ -n "${API_SERVER_KEY:-}" ] && [ "${#API_SERVER_KEY}" -ge 16 ]; then
    echo "[ordo] Hermes API server on ${API_SERVER_HOST:-127.0.0.1}:${API_SERVER_PORT:-8642} (project network only)"
else
    if [ -n "${API_SERVER_KEY:-}" ]; then
        echo "[ordo] WARNING: HERMES_API_SERVER_KEY is shorter than 16 characters; API server kept on loopback" >&2
    fi
    publish_env API_SERVER_HOST 127.0.0.1
fi

# The model: the stack's LiteLLM gateway as a NAMED provider, `ordo` (`custom:ordo`), with this
# consumer's virtual key; local-chat is the default. Named rather than the anonymous
# model.base_url endpoint because only a named provider has its model list fetched from the
# gateway's /v1/models, which is what fills the model picker (the desktop app, the dashboard);
# the anonymous one showed only the default. The old anonymous fields are removed, so there is
# one route to the gateway. Hermes still runs it as provider `custom`, which is why its timeouts
# stay under providers.custom below.
: "${LITELLM_KEY_HERMES:?LITELLM_KEY_HERMES must be set (SOPS/secrets.env) - refusing to seed a guessable default}"
CONTEXT_WINDOW="${LLAMACPP_CTX_SIZE:?LLAMACPP_CTX_SIZE is missing: it is rendered into out/.env by ordo render}"
hermes_config providers.ordo.base_url http://model-gateway:11435/v1
hermes_config providers.ordo.api_key "$LITELLM_KEY_HERMES"
hermes_config model.provider custom:ordo
hermes_config_unset model.base_url
hermes_config_unset model.api_key
hermes_config model.default local-chat
hermes_config model.context_length "$CONTEXT_WINDOW"
# Per-turn budgets, monitorable from .env. model.max_tokens: output cap per call (match
# LLAMACPP_N_PREDICT); agent.max_turns: tool-loop ceiling; agent.gateway_timeout: wall-clock cap
# on one turn (distinct from the stream-stale detector, HERMES_STREAM_STALE_TIMEOUT).
hermes_config model.max_tokens "${HERMES_MAX_TOKENS:-65536}"
hermes_config agent.max_turns "${HERMES_MAX_TURNS:-90}"
hermes_config agent.gateway_timeout "${HERMES_GATEWAY_TIMEOUT:-3600}"
hermes_config agent.api_max_retries "${HERMES_API_MAX_RETRIES:-10}"
# The proxy's /v1/models exposes no max_input_tokens, so without this Hermes assumes a 128K
# compression model and warns that it is smaller than the main model's compression threshold.
hermes_config auxiliary.compression.context_length "$CONTEXT_WINDOW"
# Every auxiliary task runs on the main model. Their default, `auto`, records usage under the
# literal provider "auto", so the dashboard's Models page split one model into several cards;
# `main` resolves to the main provider and accounts under it. The task list comes from Hermes's
# own defaults, so a task added upstream is covered without editing this file.
AUX_TASKS="$(as_hermes "$HERMES_PYTHON" -c 'from hermes_cli.config_defaults import DEFAULT_CONFIG
print(" ".join(k for k, v in DEFAULT_CONFIG["auxiliary"].items() if isinstance(v, dict) and "provider" in v))')"
for task in $AUX_TASKS; do
    hermes_config "auxiliary.${task}.provider" main
done
# Compaction trigger as a fraction of the effective input budget; seeded only when the site sets
# it. The consumed key is compression.threshold (a 0-1 fraction despite the env var's name).
if [ -n "${HERMES_COMPRESSION_THRESHOLD_PERCENT:-}" ]; then
    hermes_config compression.threshold "$HERMES_COMPRESSION_THRESHOLD_PERCENT"
fi
# Hide internal reasoning and interim assistant updates from user-facing replies (the model keeps
# its thinking internally for tool use). Seeded once, so later operator preference changes survive
# restarts like other user-owned display settings. Discord's platform-level interim setting
# overrides the global one and has its own marker, so homes seeded before it existed still get it.
DISPLAY_PREFS_MARK="$HERMES_HOME/.ordo-response-display-seeded-v1"
if [ ! -f "$DISPLAY_PREFS_MARK" ]; then
    hermes_config display.show_reasoning false
    hermes_config display.interim_assistant_messages false
    hermes_config display.platforms.discord.show_reasoning false
    as_hermes touch "$DISPLAY_PREFS_MARK"
fi
DISCORD_INTERIM_PREFS_MARK="$HERMES_HOME/.ordo-discord-interim-off-seeded-v1"
if [ ! -f "$DISCORD_INTERIM_PREFS_MARK" ]; then
    hermes_config display.platforms.discord.interim_assistant_messages off
    as_hermes touch "$DISCORD_INTERIM_PREFS_MARK"
fi
# MCP tools come from LiteLLM's MCP gateway (every mcp-* service this key is granted).
hermes_config mcp_servers.gateway.url http://model-gateway:11435/mcp
hermes_config mcp_servers.gateway.headers.Authorization "Bearer ${LITELLM_KEY_HERMES}"
# Local models prefill long contexts for minutes; the default 180s stale timeout aborts them.
# Keyed `custom`: the provider id Hermes runs the named `ordo` provider as (hermes_cli/timeouts.py).
hermes_config providers.custom.stale_timeout_seconds 1800
hermes_config providers.custom.request_timeout_seconds 1800

# First run: the stack's SOUL.md (execute, don't propose) and the ops-router plugin. Upstream's
# hook seeds its own SOUL.md into an empty home first, so ours replaces it only while it is still
# upstream's untouched copy. The sentinel keeps operator edits and plugin toggles on later starts;
# its name predates the retired push-through plugin and stays so existing homes are not re-seeded.
SEED_MARK="$HERMES_HOME/.ordo-push-through-seeded"
if [ ! -f "$SEED_MARK" ]; then
    if [ ! -s "$HERMES_HOME/SOUL.md" ] || cmp -s "$HERMES_HOME/SOUL.md" /opt/hermes/docker/SOUL.md; then
        as_hermes cp /opt/ordo-seed/SOUL.md "$HERMES_HOME/SOUL.md"
    fi
    as_hermes "$HERMES_BIN" plugins enable ops-router >/dev/null
    as_hermes touch "$SEED_MARK"
fi

# Langfuse tracing, only when the langfuse stack issued keys (fail-open: without keys the hooks
# would be inert, which reads as working tracing). The SDK is an upstream extra that Hermes's
# package manager keeps in this home's dependency environment ($HERMES_HOME/installs), the same
# step upstream's own Langfuse setup runs; it is a no-op once current and survives image bumps.
# The plugin is enabled once, behind its own sentinel, so a stack that adds langfuse later still
# gets it and a later `hermes plugins disable observability/langfuse` is never undone.
LANGFUSE_MARK="$HERMES_HOME/.ordo-langfuse-seeded"
if [ -n "${HERMES_LANGFUSE_PUBLIC_KEY:-}" ]; then
    as_hermes "$HERMES_BIN" pm install --extra langfuse >/dev/null
    if [ ! -f "$LANGFUSE_MARK" ]; then
        as_hermes "$HERMES_BIN" plugins enable observability/langfuse >/dev/null
        as_hermes touch "$LANGFUSE_MARK"
    fi
fi

# Register the repo-owned skills (/opt/ordo-skills) as a skills.external_dirs entry.
timeout 60 /command/s6-setuidgid hermes "$HERMES_PYTHON" /opt/ordo/ordo_skills.py boot

# ComfyUI's output dir and the gameplay manifest are written by both ComfyUI and the agent.
COMFYUI_OUTPUT_DIR=/workspace/data/comfyui-output
GAMEPLAY_MANIFEST="$COMFYUI_OUTPUT_DIR/gameplay_manifest.json"
if [ -d "$COMFYUI_OUTPUT_DIR" ] && ! as_hermes test -w "$COMFYUI_OUTPUT_DIR"; then
    chmod 775 "$COMFYUI_OUTPUT_DIR" || true
fi
if [ -f "$GAMEPLAY_MANIFEST" ] && ! as_hermes test -w "$GAMEPLAY_MANIFEST"; then
    chown hermes:hermes "$GAMEPLAY_MANIFEST" && chmod 664 "$GAMEPLAY_MANIFEST" || true
fi

# Two-way sync of SOUL/memories with the Obsidian vault. Best effort: a vault outage must not stop
# the agent.
timeout 60 /command/s6-setuidgid hermes "$HERMES_PYTHON" /opt/ordo/vault_federate.py || echo "[ordo] vault federation skipped" >&2
