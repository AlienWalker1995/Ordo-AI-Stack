#!/bin/sh
set -eu

CHAT_MODEL="${LLAMACPP_MODEL:-model.gguf}"
EMBED_MODEL="${LLAMACPP_EMBED_MODEL:-nomic-embed-text-v1.5.Q4_K_M.gguf}"
MASTER_KEY="${LITELLM_MASTER_KEY:-local}"

sed \
  -e "s|__CHAT_MODEL__|${CHAT_MODEL}|g" \
  -e "s|__EMBED_MODEL__|${EMBED_MODEL}|g" \
  -e "s|__MASTER_KEY__|${MASTER_KEY}|g" \
  /app/config.template.yaml > /tmp/config.yaml

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
