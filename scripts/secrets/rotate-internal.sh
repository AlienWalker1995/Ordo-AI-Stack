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
#   LITELLM_MASTER_KEY, LITELLM_DB_PASSWORD, OPS_CONTROLLER_TOKEN,
#   N8N_MCP_AUTH_TOKEN, THROUGHPUT_RECORD_TOKEN (if present),
#   OAUTH2_PROXY_COOKIE_SECRET, every LITELLM_KEY_*.
#   NEVER LITELLM_SALT_KEY (rotating it makes DB-stored credentials unreadable).
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
NEW_LITELLM="sk-$(openssl rand -hex 24)"
NEW_DBPASS=$(openssl rand -hex 24)
NEW_OPS=$(openssl rand -hex 32)
NEW_N8N_MCP=$(openssl rand -hex 32)
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
awk -v lit="$NEW_LITELLM" -v dbp="$NEW_DBPASS" -v ops="$NEW_OPS" -v n8nmcp="$NEW_N8N_MCP" \
    -v thr="$NEW_THROUGHPUT" -v cookie="$NEW_COOKIE" '
BEGIN { OFS="=" }
/^LITELLM_MASTER_KEY=/        { print "LITELLM_MASTER_KEY", lit; next }
/^LITELLM_DB_PASSWORD=/       { print "LITELLM_DB_PASSWORD", dbp; next }
/^OPS_CONTROLLER_TOKEN=/      { print "OPS_CONTROLLER_TOKEN", ops; next }
/^N8N_MCP_AUTH_TOKEN=/        { print "N8N_MCP_AUTH_TOKEN", n8nmcp; next }
/^THROUGHPUT_RECORD_TOKEN=/   { print "THROUGHPUT_RECORD_TOKEN", thr; next }
/^OAUTH2_PROXY_COOKIE_SECRET=/ { print "OAUTH2_PROXY_COOKIE_SECRET", cookie; next }
/^LITELLM_KEY_[A-Z0-9_]+=/    { split($0, kv, "="); cmd = "openssl rand -hex 24"; cmd | getline hex; close(cmd); print kv[1], "sk-" hex; next }
/^LITELLM_SALT_KEY=/          { print; next }
{ print }
' "$TMP" > "$TMP.new"

sops --encrypt --age $(grep "^# public key:" "$KEY_PATH" | awk '{print $4}') \
    --input-type=dotenv --output-type=dotenv "$TMP.new" \
    > secrets/.env.sops

cat <<EOF

==> Internal tokens rotated in secrets/.env.sops.

Next steps:
  1. Copy the rotated values into out/secrets.env (the file compose reads).
  2. cd out
     docker compose -p ordo restart model-gateway model-gateway-keys litellm-db \\
         dashboard ops-controller agent hermes-dashboard open-webui n8n oauth2-proxy
     cd ..
  3. git commit secrets/.env.sops + push.

LITELLM_DB_PASSWORD rotation also requires ALTER USER litellm PASSWORD inside
litellm-db BEFORE the restart (see docs/runbooks/secrets.md).

All existing oauth2-proxy sessions invalidate (cookie secret rotated).
You'll need to sign in via Google again.
EOF
