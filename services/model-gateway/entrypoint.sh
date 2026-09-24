#!/bin/sh
set -eu

# Fail loud: LITELLM_MASTER_KEY is the ONLY auth on the SSO-bypassing /llm + /mcp edge routes.
# Refuse a missing OR weak key (`local` ran for months). Shape: sk- followed by >= 32 chars.
: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY must be set (SOPS/secrets.env) - refusing to start with a guessable default}"
case "${LITELLM_MASTER_KEY}" in
  sk-????????????????????????????????*) ;;
  *) echo "LITELLM_MASTER_KEY must look like sk-<32+ chars> (run the wizard generator); refusing to start" >&2; exit 1 ;;
esac

# A compose `command:` (the model-gateway-keys one-shot) runs INSTEAD of the proxy, after the
# key guard so a weak/missing master key fails there too. No command -> template + run LiteLLM.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# model_info documentation values, sourced from the SAME rendered .env keys the backend llama-server
# containers read (each service declares the keys it reads, see MODEL_GATEWAY_DERIVED_ENV in
# ordo/compose.py), so the gateway's advertised metadata cannot drift from the running deployment.
# The two context windows have no default: a missing one would advertise a window the render never
# chose, so the gateway refuses to start instead.
CTX_SIZE="${LLAMACPP_CTX_SIZE:?LLAMACPP_CTX_SIZE is missing: it is rendered into out/.env by ordo render}"
N_PREDICT="${LLAMACPP_N_PREDICT:-65536}"
CPU_CTX_SIZE="${LLAMACPP_CPU_CTX:?LLAMACPP_CPU_CTX is missing: it is rendered into out/.env by ordo render}"
GPU_WEIGHTS="${LLAMACPP_MODEL:-model.gguf}"
CPU_WEIGHTS="${LLAMACPP_CPU_MODEL:-Qwen3.6-35B-A3B-UD-Q4_K_M.gguf}"
EMBED_WEIGHTS="${LLAMACPP_EMBED_MODEL:-nomic-embed-text-v1.5.Q4_K_M.gguf}"
GPU_IMAGE="${LLAMACPP_IMAGE:-llama.cpp}"

# Electricity-derived per-token cost for the local models (ordo.yaml `cost:`; see
# ordo/render.py local_token_costs). Defaults to "0" so a pre-cost .env still boots with the
# historical $0/token pricing instead of failing to start.
LOCAL_INPUT_COST_PER_TOKEN="${LOCAL_INPUT_COST_PER_TOKEN:-0}"
LOCAL_OUTPUT_COST_PER_TOKEN="${LOCAL_OUTPUT_COST_PER_TOKEN:-0}"

# The pickable pin-alias NAMES derive from the deployed weights (basename, lowercased,
# .gguf stripped) — a model swap renames them automatically, so the template never
# hardcodes a model generation. `local-chat`/`local-embed` stay stable by contract.
GPU_MODEL_NAME="$(basename "${GPU_WEIGHTS}" .gguf | tr '[:upper:]' '[:lower:]')"
CPU_MODEL_NAME="$(basename "${CPU_WEIGHTS}" .gguf | tr '[:upper:]' '[:lower:]')-cpu"

# Vision support is a fact about the deployment (is an mmproj loaded?), not the template.
if [ -n "${LLAMACPP_MMPROJ:-}" ]; then GPU_SUPPORTS_VISION=true; else GPU_SUPPORTS_VISION=false; fi

sed -e "s|__CTX_SIZE__|${CTX_SIZE}|g" \
    -e "s|__N_PREDICT__|${N_PREDICT}|g" \
    -e "s|__CPU_CTX_SIZE__|${CPU_CTX_SIZE}|g" \
    -e "s|__GPU_WEIGHTS__|${GPU_WEIGHTS}|g" \
    -e "s|__CPU_WEIGHTS__|${CPU_WEIGHTS}|g" \
    -e "s|__EMBED_WEIGHTS__|${EMBED_WEIGHTS}|g" \
    -e "s|__GPU_IMAGE__|${GPU_IMAGE}|g" \
    -e "s|__GPU_MODEL_NAME__|${GPU_MODEL_NAME}|g" \
    -e "s|__CPU_MODEL_NAME__|${CPU_MODEL_NAME}|g" \
    -e "s|__GPU_SUPPORTS_VISION__|${GPU_SUPPORTS_VISION}|g" \
    -e "s|__LOCAL_INPUT_COST_PER_TOKEN__|${LOCAL_INPUT_COST_PER_TOKEN}|g" \
    -e "s|__LOCAL_OUTPUT_COST_PER_TOKEN__|${LOCAL_OUTPUT_COST_PER_TOKEN}|g" /app/config.template.yaml > /tmp/config.yaml

# LiteLLM resolves `callbacks:` module paths relative to the CONFIG FILE's directory, and the
# config lives in /tmp (read_only container + tmpfs). Co-locate the callback with it.
cp /app/throughput_callback.py /tmp/throughput_callback.py

# Merge the render-emitted MCP server fragment (out/model-gateway/mcp_servers.yaml, mounted at
# /config). Required: a missing fragment means the mount or the render is wrong; never boot with
# a silently empty tool set. Exits 2 on a missing/invalid fragment.
python3 /app/merge_mcp_config.py /tmp/config.yaml "${MCP_SERVERS_FILE:-/config/mcp_servers.yaml}"

# Plugin-dependent logging callbacks (e.g. `langfuse_otel` when the langfuse plugin is enabled).
# The renderer sets LITELLM_EXTRA_CALLBACKS only when the plugin is on; unset/empty is a no-op.
# Fail-open: a callback whose credentials are missing is skipped with a warning, never fatal.
python3 /app/add_callbacks.py /tmp/config.yaml "${LITELLM_EXTRA_CALLBACKS:-}"

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
