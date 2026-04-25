#!/usr/bin/env bash
set -euo pipefail

# Rotate internal Ordo tokens by regenerating random values, re-encrypting
# secrets/.env.sops, and printing the restart commands. Run this when:
# - You suspect any of these tokens has leaked.
# - You're cycling out a contributor / collaborator (single-user homelab,
#   so this is mostly aspirational, but the workflow exists).
# - You're staging a fresh tailnet hostname migration.
#
# Tokens rotated:
#   LITELLM_MASTER_KEY, DASHBOARD_AUTH_TOKEN, OPS_CONTROLLER_TOKEN,
#   THROUGHPUT_RECORD_TOKEN (if present), OAUTH2_PROXY_COOKIE_SECRET.
#
# OAUTH2_PROXY_CLIENT_ID and CLIENT_SECRET are NOT rotated here —
# those require interactive Google Cloud Console action.

cd "$(dirname "$0")/../.."

KEY_DEFAULT="${HOME}/.config/sops/age/keys.txt"
KEY_PATH="${SOPS_AGE_KEY_FILE:-$KEY_DEFAULT}"

if [ ! -f "$KEY_PATH" ]; then
    echo "ERROR: age private key not found at $KEY_PATH." >&2
    exit 1
fi
export SOPS_AGE_KEY_FILE="$KEY_PATH"

# Generate fresh values.
NEW_LITELLM=$(openssl rand -hex 32)
NEW_DASHBOARD=$(openssl rand -hex 32)
NEW_OPS=$(openssl rand -hex 32)
NEW_THROUGHPUT=$(openssl rand -hex 32)
# oauth2-proxy needs exactly 16/24/32 raw bytes; generate 32 alphanumeric.
NEW_COOKIE=$(LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 32)

TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP.new"' EXIT

# Decrypt → substitute → re-encrypt.
sops --decrypt --input-type=dotenv --output-type=dotenv \
    secrets/.env.sops > "$TMP"

# In-place line-by-line substitution. Only rotate keys that ALREADY exist
# in the file — don't introduce new keys.
awk -v lit="$NEW_LITELLM" -v dash="$NEW_DASHBOARD" -v ops="$NEW_OPS" \
    -v thr="$NEW_THROUGHPUT" -v cookie="$NEW_COOKIE" '
BEGIN { OFS="=" }
/^LITELLM_MASTER_KEY=/        { print "LITELLM_MASTER_KEY", lit; next }
/^DASHBOARD_AUTH_TOKEN=/      { print "DASHBOARD_AUTH_TOKEN", dash; next }
/^OPS_CONTROLLER_TOKEN=/      { print "OPS_CONTROLLER_TOKEN", ops; next }
/^THROUGHPUT_RECORD_TOKEN=/   { print "THROUGHPUT_RECORD_TOKEN", thr; next }
/^OAUTH2_PROXY_COOKIE_SECRET=/ { print "OAUTH2_PROXY_COOKIE_SECRET", cookie; next }
{ print }
' "$TMP" > "$TMP.new"

sops --encrypt --age $(grep "^# public key:" "$KEY_PATH" | awk '{print $4}') \
    --input-type=dotenv --output-type=dotenv "$TMP.new" \
    > secrets/.env.sops

cat <<EOF

==> Internal tokens rotated in secrets/.env.sops.

Next steps:
  1. make decrypt-secrets         # write new values to ~/.ai-toolkit/runtime/.env
  2. docker compose restart \\
       model-gateway dashboard ops-controller worker hermes-gateway \\
       hermes-dashboard mcp-gateway oauth2-proxy
  3. git commit secrets/.env.sops + push.

All existing oauth2-proxy sessions invalidate (cookie secret rotated).
You'll need to sign in via Google again.
EOF
