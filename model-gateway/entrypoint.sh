#!/bin/sh
set -eu

MASTER_KEY="${LITELLM_MASTER_KEY:-local}"
CTX_SIZE="${LLAMACPP_CTX_SIZE:-262144}"

sed -e "s|__MASTER_KEY__|${MASTER_KEY}|g" \
    -e "s|__CTX_SIZE__|${CTX_SIZE}|g" /app/config.template.yaml > /tmp/config.yaml

exec litellm --config /tmp/config.yaml --host 0.0.0.0 --port 11435
