#!/usr/bin/env bash
set -euo pipefail

# Decrypt SOPS-encrypted secrets into ~/.ai-toolkit/runtime/.
# Runs at `make up`. Requires the age private key at
# ~/.config/sops/age/keys.txt (or wherever SOPS_AGE_KEY_FILE points).

cd "$(dirname "$0")/../.."

KEY_DEFAULT="${HOME}/.config/sops/age/keys.txt"
KEY_PATH="${SOPS_AGE_KEY_FILE:-$KEY_DEFAULT}"

if [ ! -f "$KEY_PATH" ]; then
    echo "ERROR: age private key not found at $KEY_PATH." >&2
    echo "Generate one: age-keygen -o $KEY_DEFAULT && chmod 600 $_" >&2
    echo "Or set SOPS_AGE_KEY_FILE to your existing key path." >&2
    exit 1
fi

# SOPS 3.7.x on Windows does not auto-detect dotenv format from file
# content; the explicit --input-type / --output-type flags are required.
export SOPS_AGE_KEY_FILE="$KEY_PATH"

RUNTIME_DIR="${HOME}/.ai-toolkit/runtime"
SECRETS_DIR="${RUNTIME_DIR}/secrets"
mkdir -p "$SECRETS_DIR"
# Best-effort lockdown; on Windows MSYS chmod is partial but harmless.
chmod 700 "$RUNTIME_DIR" "$SECRETS_DIR" 2>/dev/null || true

# Env-form: decrypt to a single .env file.
sops --decrypt --input-type=dotenv --output-type=dotenv \
    secrets/.env.sops > "${RUNTIME_DIR}/.env"
chmod 600 "${RUNTIME_DIR}/.env" 2>/dev/null || true
echo "==> ${RUNTIME_DIR}/.env (env-form internal tokens)"

# File-form: decrypt each high-value token to its own file.
for src in secrets/discord_token.sops \
           secrets/github_pat.sops \
           secrets/hf_token.sops \
           secrets/tavily_key.sops \
           secrets/civitai_token.sops; do
    [ -f "$src" ] || { echo "WARN: $src missing, skipping" >&2; continue; }
    name=$(basename "$src" .sops)
    sops --decrypt --input-type=binary --output-type=binary "$src" \
        > "${SECRETS_DIR}/${name}"
    chmod 600 "${SECRETS_DIR}/${name}" 2>/dev/null || true
    echo "==> ${SECRETS_DIR}/${name}"
done

echo ""
echo "Runtime secrets written under ${RUNTIME_DIR}."
echo "These files are outside /workspace and the HERMES_HOST_DEV_MOUNT,"
echo "so prompt-injected Hermes cannot 'cat' them."
