#!/usr/bin/env bash
# start-hermes-host.sh — Single-command bootstrap for host-mode Hermes Agent.
# Installs all dependencies (uv, Hermes, Python venv, web UI), starts Docker
# infrastructure, writes endpoint config, and launches Hermes.
#
# Usage:
#   ./scripts/start-hermes-host.sh              # Launch TUI (requires WSL2; Git Bash unsupported for TUI)
#   ./scripts/start-hermes-host.sh --dashboard  # Launch web UI at http://localhost:9119 (Git Bash OK)
#   ./scripts/start-hermes-host.sh --no-launch  # Set up everything, don't launch
set -eu
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── Pin ──
# Update deliberately; do not chase upstream main. See docs/hermes-agent.md for refresh cadence.
HERMES_REPO="https://github.com/NousResearch/hermes-agent.git"
HERMES_PINNED_SHA="${HERMES_PINNED_SHA:-dcd763c284086afd5ddee4fdcd86daaf534916ab}"
HERMES_DIR="$REPO_ROOT/vendor/hermes-agent"

# ── Arg parsing ──
LAUNCH_MODE="tui"
for arg in "$@"; do
  case "$arg" in
    --dashboard)  LAUNCH_MODE="dashboard" ;;
    --gateway)    LAUNCH_MODE="gateway" ;;   # Discord / Telegram / Slack messaging gateway
    --no-launch)  LAUNCH_MODE="none" ;;
    --tui)        LAUNCH_MODE="tui" ;;
    -h|--help)
      sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg (use --dashboard, --gateway, --tui, --no-launch)"; exit 2 ;;
  esac
done

# ── Phase 1: Load config ──
# Only pick up the specific vars this script uses. A full `source .env` breaks on
# values with shell metacharacters (e.g. COMPOSE_FILE=docker-compose.yml;overrides/compute.yml,
# where `;` is a command separator in bash). Docker Compose reads .env natively anyway.
if [ -f .env ]; then
  _hermes_env_keys=(
    MODEL_GATEWAY_PORT MCP_GATEWAY_PORT LITELLM_MASTER_KEY
    HERMES_HOME HERMES_PINNED_SHA HERMES_DASHBOARD_PORT
    DISCORD_BOT_TOKEN DISCORD_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOWED_CHANNELS
    DISCORD_ALLOWED_ROLES DISCORD_REQUIRE_MENTION DISCORD_FREE_RESPONSE_CHANNELS
    DISCORD_HOME_CHANNEL DISCORD_AUTO_THREAD DISCORD_REACTIONS
    TELEGRAM_BOT_TOKEN
  )
  for key in "${_hermes_env_keys[@]}"; do
    val=$(grep -E "^[[:space:]]*${key}=" .env | head -1 | cut -d= -f2- \
          | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")
    [ -n "$val" ] && export "$key=$val"
  done
  # Legacy alias: OpenClaw used DISCORD_TOKEN; Hermes wants DISCORD_BOT_TOKEN.
  if [ -z "${DISCORD_BOT_TOKEN:-}" ] && [ -n "${DISCORD_TOKEN:-}" ]; then
    export DISCORD_BOT_TOKEN="$DISCORD_TOKEN"
  fi
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

# ── Phase 4.3: Patch Hermes for Windows os.kill bug ──
# Hermes's gateway/status.py does `os.kill(pid, 0)` as a pid-existence check, but
# Windows raises OSError(WinError 87/11) for dead PIDs instead of ProcessLookupError.
# The upstream except-clause misses those and the CLI crashes before startup with
# a stale PID file. Idempotent in-place patch — re-clones re-apply it automatically.
STATUS_PY="$HERMES_DIR/gateway/status.py"
if [ -f "$STATUS_PY" ] && ! grep -q "Ordo AI Stack patch" "$STATUS_PY" 2>/dev/null; then
  python3 - "$STATUS_PY" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
src = p.read_text(encoding="utf-8")
# Site 1: get_running_pid — pid-file existence check.
old1 = ("    try:\n"
        "        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent\n"
        "    except (ProcessLookupError, PermissionError):\n"
        "        _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)\n"
        "        return None\n")
new1 = ("    try:\n"
        "        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent\n"
        "    except (ProcessLookupError, PermissionError):\n"
        "        _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)\n"
        "        return None\n"
        "    except OSError:\n"
        "        # Ordo AI Stack patch — Windows raises OSError(WinError 87/11) for dead PIDs\n"
        "        # instead of ProcessLookupError. Treat as stale and clean up.\n"
        "        import sys as _sys\n"
        "        if _sys.platform == 'win32':\n"
        "            _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)\n"
        "            return None\n"
        "        raise\n")
# Site 2: acquire_scoped_lock — platform-lock stale check.
old2 = ("        if not stale:\n"
        "            try:\n"
        "                os.kill(existing_pid, 0)\n"
        "            except (ProcessLookupError, PermissionError):\n"
        "                stale = True\n"
        "            else:\n")
new2 = ("        if not stale:\n"
        "            try:\n"
        "                os.kill(existing_pid, 0)\n"
        "            except (ProcessLookupError, PermissionError):\n"
        "                stale = True\n"
        "            except OSError:\n"
        "                # Ordo AI Stack patch — Windows OSError variants mean dead PID.\n"
        "                import sys as _sys\n"
        "                if _sys.platform == 'win32':\n"
        "                    stale = True\n"
        "                else:\n"
        "                    raise\n"
        "            else:\n")
changed = False
for old, new, label in ((old1, new1, "get_running_pid"), (old2, new2, "acquire_scoped_lock")):
    if old in src:
        src = src.replace(old, new, 1)
        changed = True
        print(f"patched: {label}")
    else:
        print(f"skip: {label} pattern not found (upstream changed?)")
if changed:
    p.write_text(src, encoding="utf-8")
PY
fi

# ── Phase 4.4: Install global `hermes` wrapper (user-level PATH) ──
# Put a launcher at $HOME/.local/bin so `hermes` is callable from any shell without
# cd'ing into the repo. uv installs itself there too, so it's already on PATH.
WRAPPER_BIN_DIR="$HOME/.local/bin"
mkdir -p "$WRAPPER_BIN_DIR"
# POSIX wrapper (Git Bash / WSL2). Pre-cleans gateway.pid for the gateway subcommand
# to sidestep Hermes's Windows os.kill bug if the source patch ever regresses.
cat > "$WRAPPER_BIN_DIR/hermes" <<EOF
#!/usr/bin/env bash
# Auto-generated by scripts/start-hermes-host.sh. Launches Hermes configured
# against the Ordo AI Stack repo at $REPO_ROOT.
export HERMES_HOME="\${HERMES_HOME:-$REPO_ROOT/data/hermes}"
export PYTHONIOENCODING="\${PYTHONIOENCODING:-utf-8}"
[ "\$1" = "gateway" ] && rm -f "\$HERMES_HOME/gateway.pid"
exec "$HERMES_DIR/.venv/Scripts/hermes.exe" "\$@" 2>/dev/null || \\
exec "$HERMES_DIR/.venv/bin/hermes" "\$@"
EOF
chmod +x "$WRAPPER_BIN_DIR/hermes"
# Windows PowerShell / cmd wrapper — needs true Windows paths, not Git Bash /c/... form.
if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT_WIN=$(cygpath -w "$REPO_ROOT")
  HERMES_DIR_WIN=$(cygpath -w "$HERMES_DIR")
else
  REPO_ROOT_WIN="$REPO_ROOT"
  HERMES_DIR_WIN="$HERMES_DIR"
fi
cat > "$WRAPPER_BIN_DIR/hermes.cmd" <<EOF
@echo off
REM Auto-generated by scripts/start-hermes-host.sh.
set HERMES_HOME=${REPO_ROOT_WIN}\\data\\hermes
set PYTHONIOENCODING=utf-8
if /I "%~1"=="gateway" if exist "%HERMES_HOME%\\gateway.pid" del /Q "%HERMES_HOME%\\gateway.pid"
"${HERMES_DIR_WIN}\\.venv\\Scripts\\hermes.exe" %*
EOF
echo "wrapper: $WRAPPER_BIN_DIR/hermes (.cmd)"

# ── Phase 4.5: Build web UI if missing ──
# Hermes dashboard serves assets from vendor/hermes-agent/hermes_cli/web_dist/.
# The web/ package prebuild uses `rm -rf` and `cp -r` which require POSIX (Git Bash / WSL2 / Linux).
WEB_DIST="$HERMES_DIR/hermes_cli/web_dist"
if [ ! -f "$WEB_DIST/index.html" ]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "!! npm not found — needed to build the Hermes web UI."
    echo "   Install Node.js (https://nodejs.org/) then re-run."
    exit 1
  fi
  echo "==> Installing web UI dependencies (one-time, ~100MB)..."
  (cd "$HERMES_DIR/web" && npm install)
  echo "==> Building web UI..."
  (cd "$HERMES_DIR/web" && npm run build)
fi

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

# ── Phase 7.5: Seed $HERMES_HOME/.env for gateway ──
# Hermes reads its own .env at $HERMES_HOME/.env at gateway startup. Copy the
# relevant Discord/Telegram vars from the repo .env so `hermes gateway` picks them
# up regardless of how it's invoked (bootstrap, wrapper, or direct).
HERMES_ENV="$HERMES_HOME/.env"
{
  echo "# Auto-managed by scripts/start-hermes-host.sh — Hermes reads this at gateway startup."
  echo "# Regenerated on each bootstrap run. Do not commit."
  for key in DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOWED_CHANNELS \
             DISCORD_ALLOWED_ROLES DISCORD_REQUIRE_MENTION DISCORD_FREE_RESPONSE_CHANNELS \
             DISCORD_HOME_CHANNEL DISCORD_AUTO_THREAD DISCORD_REACTIONS \
             TELEGRAM_BOT_TOKEN; do
    # Pull the in-process value (Phase 1 exported these). Skip empties.
    val=$(eval "printf '%s' \"\${$key:-}\"")
    [ -n "$val" ] && echo "$key=$val"
  done
} > "$HERMES_ENV"
echo "seeded: $HERMES_ENV"

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
case "$LAUNCH_MODE" in
  dashboard)
    DASH_PORT="${HERMES_DASHBOARD_PORT:-9119}"
    if curl -sf -m 1 "http://localhost:$DASH_PORT/" >/dev/null 2>&1; then
      echo "!! Port $DASH_PORT already serving a Hermes dashboard."
      echo "   Open http://localhost:$DASH_PORT/ in your browser, or stop the existing"
      echo "   instance and re-run. To find the PID (Windows):"
      echo "     netstat -ano | grep :$DASH_PORT"
      exit 0
    fi
    echo "==> Launching Hermes dashboard at http://localhost:$DASH_PORT/ ..."
    exec "$HERMES_BIN" dashboard --port "$DASH_PORT" --no-open
    ;;
  gateway)
    # Messaging gateway — Discord, Telegram, Slack. Persistent process; leave it running.
    if [ -z "${DISCORD_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
      echo "!! No messaging platform configured."
      echo "   Set DISCORD_BOT_TOKEN (or legacy DISCORD_TOKEN) and DISCORD_ALLOWED_USERS"
      echo "   in .env — see the 'Hermes Discord Gateway' block in .env.example."
      exit 2
    fi
    if [ -n "${DISCORD_BOT_TOKEN:-}" ] && [ -z "${DISCORD_ALLOWED_USERS:-}" ]; then
      echo "!! DISCORD_BOT_TOKEN is set but DISCORD_ALLOWED_USERS is empty."
      echo "   Without it the bot denies all users by default. Set it in .env"
      echo "   (your Discord user ID from Settings → Advanced → Developer Mode)."
      exit 2
    fi
    # Pre-clean stale PID file. Hermes's get_running_pid() does an `os.kill(pid, 0)`
    # existence check; on Windows a dead-PID lookup raises OSError(WinError 87) which
    # escapes the `except (ProcessLookupError, PermissionError)` and crashes the CLI
    # before it can start. If the PID file references a dead process, just remove it.
    # (If there's actually a running gateway we'd notice: start would fail on port bind.)
    rm -f "$HERMES_HOME/gateway.pid"
    echo "==> Launching Hermes messaging gateway..."
    [ -n "${DISCORD_BOT_TOKEN:-}" ] && echo "    Discord: enabled (users: ${DISCORD_ALLOWED_USERS:-<NONE>})"
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && echo "    Telegram: enabled"
    echo "    This is a persistent process. Ctrl+C to stop."
    exec "$HERMES_BIN" gateway
    ;;
  tui)
    echo "==> Launching Hermes CLI (HERMES_HOME=$HERMES_HOME)..."
    echo "    TUI requires a Windows console or POSIX terminal. Git Bash will crash"
    echo "    on prompt_toolkit; use WSL2, or re-run with --dashboard for the web UI."
    exec "$HERMES_BIN"
    ;;
  none)
    echo "==> Setup complete. Launch manually:"
    echo "    hermes                       # TUI (WSL2)"
    echo "    hermes dashboard             # Web UI at http://localhost:9119"
    echo "    hermes gateway               # Discord/Telegram gateway (needs DISCORD_BOT_TOKEN + DISCORD_ALLOWED_USERS)"
    ;;
esac
