#!/bin/sh
set -eu

set -- \
  --host 0.0.0.0 \
  --port 8080 \
  --model "/models/${LLAMACPP_MODEL:-model.gguf}" \
  --ctx-size "${LLAMACPP_CTX_SIZE:-262144}" \
  --parallel "${LLAMACPP_PARALLEL:-1}" \
  --rope-scaling "${LLAMACPP_ROPE_SCALING:-none}" \
  --rope-scale "${LLAMACPP_ROPE_SCALE:-1}" \
  --yarn-orig-ctx "${LLAMACPP_YARN_ORIG_CTX:-0}" \
  --n-gpu-layers "${LLAMACPP_GPU_LAYERS:--1}" \
  --flash-attn "${LLAMACPP_FLASH_ATTN:-auto}" \
  --jinja \
  --no-mmap

if [ -n "${LLAMACPP_OVERRIDE_KV:-}" ]; then
  set -- "$@" --override-kv "${LLAMACPP_OVERRIDE_KV}"
fi

if [ "${LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION:-0}" = "1" ]; then
  set -- "$@" \
    --cache-type-k "${LLAMACPP_KV_CACHE_TYPE_K:-q4_0}" \
    --cache-type-v "${LLAMACPP_KV_CACHE_TYPE_V:-q4_0}"

  # TurboQuant (tbq*_N / tbqp*_N) requires Flash Attention — without FA the
  # rotation-quantize kernels silently corrupt KV. Append --flash-attn on
  # so llama-server's last-wins arg parsing overrides any earlier
  # `auto`/`off` value.
  case "${LLAMACPP_KV_CACHE_TYPE_K:-}${LLAMACPP_KV_CACHE_TYPE_V:-}" in
    *tbq*) set -- "$@" --flash-attn on ;;
  esac
fi

if [ -n "${LLAMACPP_EXTRA_ARGS:-}" ]; then
  # Intentionally split LLAMACPP_EXTRA_ARGS on whitespace so operators can append
  # raw llama-server flags from .env without changing compose.
  # shellcheck disable=SC2086
  set -- "$@" ${LLAMACPP_EXTRA_ARGS}
fi

echo "llama-server args: $*"
exec /app/llama-server "$@"
