#!/bin/sh
set -eu

# Fail loud: LITELLM_MASTER_KEY is the ONLY auth on the SSO-bypassing /llm edge
# route. Refuse to start on an empty secret rather than bake a guessable default.
: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY must be set (SOPS/secrets.env) — refusing to start with a guessable default}"

MASTER_KEY="${LITELLM_MASTER_KEY}"
CTX_SIZE="${LLAMACPP_CTX_SIZE:-262144}"

sed -e "s|__MASTER_KEY__|${MASTER_KEY}|g" \
    -e "s|__CTX_SIZE__|${CTX_SIZE}|g" /app/config.template.yaml > /tmp/config.yaml

# LiteLLM's proxy callback importer (get_instance_fn in
# litellm/proxy/types_utils/utils.py) resolves "module.attr" relative to the
# CONFIG FILE's directory — not sys.path. Our config lives in /tmp (compose
# mounts tmpfs there because the container is read_only:true), so the callback
# module has to be co-located. Copy from the in-image canonical location.
cp /usr/lib/python3.13/site-packages/throughput_callback.py /tmp/throughput_callback.py

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
