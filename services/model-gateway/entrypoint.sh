#!/bin/sh
set -eu

# Fail loud: LITELLM_MASTER_KEY is the ONLY auth on the SSO-bypassing /llm edge
# route. Refuse to start on an empty secret rather than bake a guessable default.
: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY must be set (SOPS/secrets.env) — refusing to start with a guessable default}"

MASTER_KEY="${LITELLM_MASTER_KEY}"

# model_info documentation values — sourced from the SAME env vars the backend llama-server
# containers read (shared .env via env_file), so the gateway's advertised metadata cannot
# drift from the running deployment. Defaults mirror the compose/run-script defaults exactly.
CTX_SIZE="${LLAMACPP_CTX_SIZE:-262144}"
N_PREDICT="${LLAMACPP_N_PREDICT:-65536}"
CPU_CTX_SIZE="${LLAMACPP_CPU_CTX:-131072}"
GPU_WEIGHTS="${LLAMACPP_MODEL:-model.gguf}"
CPU_WEIGHTS="${LLAMACPP_CPU_MODEL:-Qwen3.6-35B-A3B-UD-Q4_K_M.gguf}"
EMBED_WEIGHTS="${LLAMACPP_EMBED_MODEL:-nomic-embed-text-v1.5.Q4_K_M.gguf}"
GPU_IMAGE="${LLAMACPP_IMAGE:-llama.cpp}"

# The pickable pin-alias NAMES derive from the deployed weights (basename, lowercased,
# .gguf stripped) — a model swap renames them automatically, so the template never
# hardcodes a model generation. `local-chat`/`local-embed` stay stable by contract.
GPU_MODEL_NAME="$(basename "${GPU_WEIGHTS}" .gguf | tr '[:upper:]' '[:lower:]')"
CPU_MODEL_NAME="$(basename "${CPU_WEIGHTS}" .gguf | tr '[:upper:]' '[:lower:]')-cpu"

# Vision support is a fact about the deployment (is an mmproj loaded?), not the template.
if [ -n "${LLAMACPP_MMPROJ:-}" ]; then GPU_SUPPORTS_VISION=true; else GPU_SUPPORTS_VISION=false; fi

sed -e "s|__MASTER_KEY__|${MASTER_KEY}|g" \
    -e "s|__CTX_SIZE__|${CTX_SIZE}|g" \
    -e "s|__N_PREDICT__|${N_PREDICT}|g" \
    -e "s|__CPU_CTX_SIZE__|${CPU_CTX_SIZE}|g" \
    -e "s|__GPU_WEIGHTS__|${GPU_WEIGHTS}|g" \
    -e "s|__CPU_WEIGHTS__|${CPU_WEIGHTS}|g" \
    -e "s|__EMBED_WEIGHTS__|${EMBED_WEIGHTS}|g" \
    -e "s|__GPU_IMAGE__|${GPU_IMAGE}|g" \
    -e "s|__GPU_MODEL_NAME__|${GPU_MODEL_NAME}|g" \
    -e "s|__CPU_MODEL_NAME__|${CPU_MODEL_NAME}|g" \
    -e "s|__GPU_SUPPORTS_VISION__|${GPU_SUPPORTS_VISION}|g" /app/config.template.yaml > /tmp/config.yaml

# LiteLLM's proxy callback importer (get_instance_fn in
# litellm/proxy/types_utils/utils.py) resolves "module.attr" relative to the
# CONFIG FILE's directory — not sys.path. Our config lives in /tmp (compose
# mounts tmpfs there because the container is read_only:true), so the callback
# module has to be co-located. Copy from the in-image canonical location.
cp /usr/lib/python3.13/site-packages/throughput_callback.py /tmp/throughput_callback.py

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
