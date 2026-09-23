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
#   THROUGHPUT_RECORD_TOKEN (if present),
#   OAUTH2_PROXY_COOKIE_SECRET, every LITELLM_KEY_*, HERMES_API_SERVER_KEY,
#   LANGFUSE_DB_PASSWORD, LANGFUSE_CLICKHOUSE_PASSWORD, LANGFUSE_REDIS_AUTH,
#   LANGFUSE_MINIO_SECRET, LANGFUSE_NEXTAUTH_SECRET.
#   NEVER LITELLM_SALT_KEY (rotating it makes DB-stored credentials unreadable).
#   NEVER LANGFUSE_SALT or LANGFUSE_ENCRYPTION_KEY, for the same reason: SALT hashes
#     the API keys Langfuse stores (rotating it means no presented key ever matches
#     again) and ENCRYPTION_KEY encrypts its at-rest secrets (rotating it makes the
#     stored ciphertext undecryptable). Both are generate-once-per-install values.
#   NOT LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY: those are the project key pair
#     Langfuse ISSUED and stores server-side. Changing the file does not change the
#     server's copy - it just stops Hermes authenticating. Rotate them in the Langfuse
#     UI (project settings -> API keys) and copy the new pair into secrets.env.
#   NOT LANGFUSE_ADMIN_PASSWORD: it seeds the login only on the FIRST boot against an
#     empty database (LANGFUSE_INIT_*); afterwards the password lives hashed in
#     Postgres and a new value here is inert. Change it in the Langfuse UI.
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
NEW_THROUGHPUT=$(openssl rand -hex 32)
# oauth2-proxy needs exactly 16/24/32 raw bytes; generate 32 alphanumeric.
NEW_COOKIE=$(head -c 4096 </dev/urandom | LC_ALL=C tr -dc 'a-zA-Z0-9' | head -c 32)
# Langfuse infra credentials (hex: safe inside DATABASE_URL, --requirepass and S3 creds alike).
NEW_LF_DBPASS=$(openssl rand -hex 24)
NEW_LF_CLICKHOUSE=$(openssl rand -hex 24)
NEW_LF_REDIS=$(openssl rand -hex 24)
NEW_LF_MINIO=$(openssl rand -hex 24)
NEW_LF_NEXTAUTH=$(openssl rand -hex 32)
# Hermes API server bearer (the evals runner's credential). Hermes rejects anything under 16 chars.
NEW_HERMES_API=$(openssl rand -hex 32)

TMP=$(mktemp)
trap 'rm -f "$TMP" "$TMP.new"' EXIT

# Decrypt → substitute → re-encrypt.
sops --decrypt --input-type=dotenv --output-type=dotenv \
    secrets/.env.sops > "$TMP"

# In-place line-by-line substitution. Only rotate keys that ALREADY exist
# in the file — don't introduce new keys.
awk -v lit="$NEW_LITELLM" -v dbp="$NEW_DBPASS" -v ops="$NEW_OPS" \
    -v thr="$NEW_THROUGHPUT" -v cookie="$NEW_COOKIE" \
    -v lfdb="$NEW_LF_DBPASS" -v lfch="$NEW_LF_CLICKHOUSE" -v lfrd="$NEW_LF_REDIS" \
    -v lfmi="$NEW_LF_MINIO" -v lfna="$NEW_LF_NEXTAUTH" -v hapi="$NEW_HERMES_API" '
BEGIN { OFS="=" }
/^LITELLM_MASTER_KEY=/        { print "LITELLM_MASTER_KEY", lit; next }
/^LITELLM_DB_PASSWORD=/       { print "LITELLM_DB_PASSWORD", dbp; next }
/^OPS_CONTROLLER_TOKEN=/      { print "OPS_CONTROLLER_TOKEN", ops; next }
/^THROUGHPUT_RECORD_TOKEN=/   { print "THROUGHPUT_RECORD_TOKEN", thr; next }
/^OAUTH2_PROXY_COOKIE_SECRET=/ { print "OAUTH2_PROXY_COOKIE_SECRET", cookie; next }
/^LITELLM_KEY_[A-Z0-9_]+=/    { split($0, kv, "="); cmd = "openssl rand -hex 24"; cmd | getline hex; close(cmd); print kv[1], "sk-" hex; next }
/^LITELLM_SALT_KEY=/          { print; next }
/^LANGFUSE_DB_PASSWORD=/         { print "LANGFUSE_DB_PASSWORD", lfdb; next }
/^LANGFUSE_CLICKHOUSE_PASSWORD=/ { print "LANGFUSE_CLICKHOUSE_PASSWORD", lfch; next }
/^LANGFUSE_REDIS_AUTH=/          { print "LANGFUSE_REDIS_AUTH", lfrd; next }
/^LANGFUSE_MINIO_SECRET=/        { print "LANGFUSE_MINIO_SECRET", lfmi; next }
/^LANGFUSE_NEXTAUTH_SECRET=/     { print "LANGFUSE_NEXTAUTH_SECRET", lfna; next }
/^HERMES_API_SERVER_KEY=/        { print "HERMES_API_SERVER_KEY", hapi; next }
/^LANGFUSE_SALT=/                { print; next }
/^LANGFUSE_ENCRYPTION_KEY=/      { print; next }
{ print }
' "$TMP" > "$TMP.new"

sops --encrypt --age $(grep "^# public key:" "$KEY_PATH" | awk '{print $4}') \
    --input-type=dotenv --output-type=dotenv "$TMP.new" \
    > secrets/.env.sops

cat <<EOF

==> Internal tokens rotated in secrets/.env.sops.

Next steps:
  1. Copy the rotated values into out/secrets.env (the file compose reads).
  2. Recreate (not restart: a restart keeps the old environment), from the repo root:
     ordo recreate model-gateway model-gateway-keys litellm-db dashboard \\
         ops-controller agent hermes-dashboard open-webui n8n oauth2-proxy
  3. git commit secrets/.env.sops + push.

LITELLM_DB_PASSWORD rotation also requires ALTER USER litellm PASSWORD inside
litellm-db BEFORE the restart (see docs/runbooks/secrets.md).

If the langfuse plugin is enabled, its rotated credentials need the same
before-restart step on the two stores that persist their own copy:
  ALTER USER langfuse PASSWORD '<new>'                    inside langfuse-db
  ALTER USER clickhouse IDENTIFIED BY '<new>'             inside langfuse-clickhouse
then recreate the profile so redis/minio pick up their new env:
  ordo recreate langfuse-db langfuse-clickhouse langfuse-redis langfuse-minio \\
      langfuse-worker langfuse-web
Rotating LANGFUSE_NEXTAUTH_SECRET invalidates open Langfuse sessions (sign in again).

Rotating HERMES_API_SERVER_KEY requires recreating the agent so Hermes picks up the new
bearer (ordo recreate agent); eval runs must use the new value.

All existing oauth2-proxy sessions invalidate (cookie secret rotated).
You'll need to sign in via Google again.
EOF
