#!/usr/bin/env bash
# start-hermes-host.sh — Single-command bootstrap for host-mode Hermes Agent.
# Installs Hermes (if missing), starts Docker infrastructure, launches Hermes CLI.
set -eu
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Pin ──
# Update deliberately; do not chase upstream main. See docs/hermes-agent.md for refresh cadence.
HERMES_REPO="https://github.com/NousResearch/hermes-agent.git"
HERMES_PINNED_SHA="${HERMES_PINNED_SHA:-dcd763c284086afd5ddee4fdcd86daaf534916ab}"
HERMES_DIR="$REPO_ROOT/vendor/hermes-agent"

# ── Phase 1: Load config ──
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# ── Phase 2: Ensure uv ──
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv (astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "uv: $(uv --version 2>/dev/null || echo 'installed')"

# ── Phase 3: Clone Hermes if missing ──
if [ ! -d "$HERMES_DIR/.git" ]; then
  echo "==> Cloning hermes-agent..."
  mkdir -p "$(dirname "$HERMES_DIR")"
  git clone "$HERMES_REPO" "$HERMES_DIR"
fi
(cd "$HERMES_DIR" && git fetch --quiet origin && git checkout --quiet "$HERMES_PINNED_SHA")

# ── Phase 4: Install Hermes if venv missing ──
HERMES_BIN_POSIX="$HERMES_DIR/.venv/bin/hermes"
HERMES_BIN_WIN="$HERMES_DIR/.venv/Scripts/hermes.exe"
if [ ! -x "$HERMES_BIN_POSIX" ] && [ ! -x "$HERMES_BIN_WIN" ]; then
  echo "==> Installing hermes-agent into venv..."
  (cd "$HERMES_DIR" && uv venv --python 3.11 && uv pip install -e ".[all]")
fi
HERMES_BIN="$HERMES_BIN_POSIX"
[ -x "$HERMES_BIN_WIN" ] && HERMES_BIN="$HERMES_BIN_WIN"

# ── Phase 5: Start Docker infrastructure ──
echo "==> Starting Docker stack..."
docker compose up -d

# ── Phase 6: Wait for services ──
echo "==> Waiting for services..."
until curl -sf "http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1/models" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-local}" >/dev/null 2>&1; do sleep 3; done
echo "  model-gateway: OK"
until curl -sf http://localhost:8080/api/health >/dev/null 2>&1; do sleep 3; done
echo "  dashboard: OK"
until curl -sf "http://localhost:${MCP_GATEWAY_PORT:-8811}/health" >/dev/null 2>&1; do sleep 3; done
echo "  mcp-gateway: OK"

# ── Phase 7: Host-mode env vars ──
export HERMES_HOME="${HERMES_HOME:-$REPO_ROOT/data/hermes}"
mkdir -p "$HERMES_HOME"
export OPENAI_API_BASE="http://localhost:${MODEL_GATEWAY_PORT:-11435}/v1"
export OPENAI_API_KEY="${LITELLM_MASTER_KEY:-local}"
# Hermes prints UTF-8 checkmarks (\u2713) in `config set`; Windows cp1252 console can't encode them.
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# ── Phase 8: Persist Hermes endpoint config ──
# Verified against Hermes 0.10.0 (2026.4.16) source. The inline `model` dict
# (provider=custom + base_url + api_key + default) is what actually routes to the
# configured endpoint; the `providers.<name>` dict form does NOT flow through chat
# client provider resolution and falls back to openrouter.
# MCP: streamable-http transport (natively supported).
# Honcho: no disable flag; stays dormant unless ~/.honcho/config.json exists.
echo "==> Configuring Hermes endpoints..."
"$HERMES_BIN" config set model.provider          "custom"
"$HERMES_BIN" config set model.base_url          "$OPENAI_API_BASE"
"$HERMES_BIN" config set model.api_key           "$OPENAI_API_KEY"
"$HERMES_BIN" config set model.default           "local-chat"
"$HERMES_BIN" config set mcp_servers.gateway.url "http://localhost:${MCP_GATEWAY_PORT:-8811}/mcp"

# ── Phase 9: Launch ──
cd "$REPO_ROOT"
echo "==> Launching Hermes CLI (HERMES_HOME=$HERMES_HOME)..."
exec "$HERMES_BIN"
