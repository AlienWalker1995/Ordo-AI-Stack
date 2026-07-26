#!/bin/sh
# Ordo — one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/AlienWalker1995/Ordo-AI-Stack/main/install.sh | sh
#
# Takes a fresh machine from nothing to a configured stack: checks prerequisites, clones the repo,
# installs the `ordo` CLI into a virtualenv, then launches the interactive setup wizard
# (`ordo init`) which asks about hardware, model, capabilities, tailnet + Google SSO, and secrets,
# and offers to render + bring the stack up. POSIX sh; idempotent; safe to re-run.
set -eu

REPO_URL="${ORDO_REPO_URL:-https://github.com/AlienWalker1995/Ordo-AI-Stack.git}"
DEFAULT_DIR="${ORDO_DIR:-$HOME/ordo}"

info() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!  \033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Find an interactive input source. Under `curl | sh` stdin is the script itself (not a terminal),
# but /dev/tty still reaches the real terminal — so a piped install can STILL be fully interactive.
if [ -t 0 ]; then
    TTY_IN="/dev/stdin"; INTERACTIVE=1
elif { true </dev/tty; } 2>/dev/null; then
    TTY_IN="/dev/tty"; INTERACTIVE=1
else
    TTY_IN=""; INTERACTIVE=0
fi

ask() {
    # ask "Prompt" "default" -> echoes answer (default when non-interactive or empty)
    _prompt="$1"; _default="$2"
    if [ "$INTERACTIVE" -eq 1 ]; then
        printf '%s [%s]: ' "$_prompt" "$_default" >&2
        read -r _ans <"$TTY_IN" || _ans=""
        [ -n "$_ans" ] && printf '%s' "$_ans" || printf '%s' "$_default"
    else
        printf '%s' "$_default"
    fi
}

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
info "Checking prerequisites"

have git || die "git not found — install it and re-run."
have docker || die "Docker not found — install Docker (https://docs.docker.com/get-docker/) and re-run."
if ! docker compose version >/dev/null 2>&1; then
    die "'docker compose' (v2) not found — install the Compose plugin and re-run."
fi

PYTHON=""
for c in python3 python; do
    if have "$c"; then
        if "$c" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
        then PYTHON="$c"; break; fi
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11+ not found — install it and re-run."

if have nvidia-smi; then
    info "NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
else
    warn "nvidia-smi not found — the stack will run CPU-only (no image/video/voice). Continuing."
fi

# ── 2. Locate or clone the repo ──────────────────────────────────────────────
# If we're already inside a clone (this script lives in it), install from here — don't re-clone.
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd 2>/dev/null || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/ordo" ]; then
    TARGET="$SCRIPT_DIR"
    info "Using existing clone at $TARGET"
else
    TARGET="$(ask 'Install directory' "$DEFAULT_DIR")"
    if [ -d "$TARGET/.git" ] && [ -f "$TARGET/pyproject.toml" ]; then
        info "Repo already present at $TARGET — pulling latest"
        git -C "$TARGET" pull --ff-only || warn "git pull failed (local changes?) — using the existing checkout."
    elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
        die "$TARGET exists and is not an Ordo clone — choose another dir (ORDO_DIR=...) and re-run."
    else
        info "Cloning $REPO_URL -> $TARGET"
        git clone --depth 1 "$REPO_URL" "$TARGET" || die "git clone failed."
    fi
fi

cd "$TARGET"

# ── 3. Install the ordo CLI into a virtualenv ────────────────────────────────
info "Installing the ordo CLI (virtualenv .venv)"
if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv || die "failed to create virtualenv — is the python venv module installed?"
fi
# shellcheck disable=SC1091
. .venv/bin/activate 2>/dev/null || . .venv/Scripts/activate  # Scripts/ on Git-Bash/Windows
python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
python -m pip install --quiet . || die "pip install failed."

have ordo || die "the 'ordo' command did not install — check the pip output above."
info "ordo installed: $(ordo --help >/dev/null 2>&1 && echo ok)"

# ── 4. Run the wizard ────────────────────────────────────────────────────────
info "Launching the setup wizard"
if [ "$INTERACTIVE" -eq 1 ]; then
    # Redirect the wizard's stdin from the real terminal so its prompts work even when this
    # script was itself piped in (curl | sh consumes stdin; /dev/tty still reaches the console).
    ordo init --out out <"$TTY_IN"
else
    # Truly headless (no terminal at all, e.g. CI): write config non-interactively.
    warn "No terminal available — writing config non-interactively (no bring-up)."
    ordo init --yes --out out
    cat <<EOF

Config written to $TARGET/out/. To finish interactively:
  cd "$TARGET" && . .venv/bin/activate && ordo init --out out --force
Or continue manually:
  ordo --source out/ordo.yaml render --out out
  cd out && docker compose -p ordo --env-file .env --env-file secrets.env up -d
EOF
fi

info "Done. Repo: $TARGET"
