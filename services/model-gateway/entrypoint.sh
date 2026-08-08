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

sed -e "s|__MASTER_KEY__|${MASTER_KEY}|g" \
    -e "s|__CTX_SIZE__|${CTX_SIZE}|g" \
    -e "s|__N_PREDICT__|${N_PREDICT}|g" \
    -e "s|__CPU_CTX_SIZE__|${CPU_CTX_SIZE}|g" \
    -e "s|__GPU_WEIGHTS__|${GPU_WEIGHTS}|g" \
    -e "s|__CPU_WEIGHTS__|${CPU_WEIGHTS}|g" \
    -e "s|__EMBED_WEIGHTS__|${EMBED_WEIGHTS}|g" /app/config.template.yaml > /tmp/config.yaml

# LiteLLM's proxy callback importer (get_instance_fn in
# litellm/proxy/types_utils/utils.py) resolves "module.attr" relative to the
# CONFIG FILE's directory — not sys.path. Our config lives in /tmp (compose
# mounts tmpfs there because the container is read_only:true), so the callback
# module has to be co-located. Copy from the in-image canonical location.
cp /usr/lib/python3.13/site-packages/throughput_callback.py /tmp/throughput_callback.py

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
