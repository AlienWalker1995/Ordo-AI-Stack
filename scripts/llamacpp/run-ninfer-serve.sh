#!/bin/sh
set -eu

# Launcher for the GPU chat service when the active catalog model has `backend: ninfer` (the render
# picks this script over run-llama-server.sh; same service, network alias, port and models volume).
# It turns the rendered LLAMACPP_* env into the `ninfer-serve` argv:
#
#   LLAMACPP_MODEL            the .ninfer artifact in /models; also the public model id, so
#                             /v1/models names the file (the dashboard reads it) and the gateway's
#                             requests, which send the GPU weights file as `model`, match it exactly
#   LLAMACPP_CTX_SIZE         the per-request window AND the KV pool: one active request gets the
#                             whole window, as llama.cpp's single slot does
#   LLAMACPP_N_PREDICT        the output limit when a request sends no max_tokens
#   LLAMACPP_REASONING_BUDGET the default thinking-token cap for thinking-mode requests
#   LLAMACPP_VISION           "1" loads the artifact's vision encoder
#   LLAMACPP_EXTRA_ARGS       the model's catalog flags (KV dtype, MTP, concurrency, thinking options)
#
# The llama.cpp-only keys the service also receives (GPU layers, flash-attn, RoPE, KV cache types,
# parallel slots, mmproj, MoE placement) have no NInfer meaning and are ignored here. NInfer has no
# Prometheus endpoint, so there is no --metrics; ops-controller probes /health instead.

MODEL="${LLAMACPP_MODEL:?LLAMACPP_MODEL is missing: it is rendered into out/.env by ordo render}"
CTX="${LLAMACPP_CTX_SIZE:?LLAMACPP_CTX_SIZE is missing: it is rendered into out/.env by ordo render}"

# shellcheck disable=SC2086
set -- "/models/${MODEL}" \
  --host 0.0.0.0 \
  --port 8080 \
  --model-id "${MODEL}" \
  --max-context "${CTX}" \
  --kv-capacity "${CTX}" \
  --default-max-tokens "${LLAMACPP_N_PREDICT:-65536}" \
  --default-thinking-budget "${LLAMACPP_REASONING_BUDGET:-32768}"

if [ "${LLAMACPP_VISION:-0}" = "1" ]; then
  set -- "$@" --vision
fi

if [ -n "${LLAMACPP_EXTRA_ARGS:-}" ]; then
  # Split on whitespace on purpose: the catalog's flags string.
  # shellcheck disable=SC2086
  set -- "$@" ${LLAMACPP_EXTRA_ARGS}
fi

echo "ninfer-serve args: $*"
exec /usr/local/bin/ninfer-serve "$@"
