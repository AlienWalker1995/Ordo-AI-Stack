#!/bin/sh
set -eu

# Capture the container CMD (docker `command:`) BEFORE we rebuild the arg list.
# Compose renders `command: [--metrics]` (compose.LLAMACPP_METRICS_ARG) to enable
# llama-server's native Prometheus /metrics endpoint that prometheus scrapes. The
# `set --` below reassigns "$@" and would otherwise silently discard those CMD
# args, leaving llama-server without --metrics (endpoint returns 501). Preserve
# them and re-append at the end so last-wins parsing keeps operator intent.
LLAMACPP_CMD_ARGS="$*"

# Load the whole model into memory instead of memory-mapping it. The flag was renamed upstream:
# llama.cpp v0.5.0 and later (the patched image, services/llamacpp-patched) take `--load-mode none`
# and reject `--no-mmap`; the older pinned upstream images (ordo/render/llamacpp_backend.py) only
# know `--no-mmap`. Ask the binary which one it accepts rather than assume the image.
if /app/llama-server --help 2>&1 | grep -q -- '--load-mode'; then
  HAS_LOAD_MODE=1
  LOAD_ARGS="--load-mode none"
else
  HAS_LOAD_MODE=0
  LOAD_ARGS="--no-mmap"
fi

# A model that declares `load_mode: mmap-lazy` (catalog) memory-maps its weights and reads the rows of
# its very large lookup tensors (a per-layer n-gram embedding table) from disk on demand. Only builds
# with --load-mode have the lazy reader: refuse rather than silently load the table into memory.
case "${LLAMACPP_LOAD_MODE:-none}" in
  none) ;;
  mmap-lazy)
    if [ "$HAS_LOAD_MODE" != 1 ]; then
      echo "LLAMACPP_LOAD_MODE=mmap-lazy needs a llama.cpp build with --load-mode/--lazy-mode" >&2
      exit 1
    fi
    LOAD_ARGS="--load-mode mmap --lazy-mode on" ;;
  *)
    echo "unknown LLAMACPP_LOAD_MODE='${LLAMACPP_LOAD_MODE}' (none or mmap-lazy)" >&2
    exit 1 ;;
esac

# shellcheck disable=SC2086
set -- \
  --host 0.0.0.0 \
  --port 8080 \
  --model "/models/${LLAMACPP_MODEL:-model.gguf}" \
  --ctx-size "${LLAMACPP_CTX_SIZE:?LLAMACPP_CTX_SIZE is missing: it is rendered into out/.env by ordo render}" \
  --parallel "${LLAMACPP_PARALLEL:-1}" \
  --rope-scaling "${LLAMACPP_ROPE_SCALING:-none}" \
  --rope-scale "${LLAMACPP_ROPE_SCALE:-1}" \
  --yarn-orig-ctx "${LLAMACPP_YARN_ORIG_CTX:-0}" \
  --n-gpu-layers "${LLAMACPP_GPU_LAYERS:--1}" \
  --flash-attn "${LLAMACPP_FLASH_ATTN:-auto}" \
  --n-predict "${LLAMACPP_N_PREDICT:-65536}" \
  --reasoning-budget "${LLAMACPP_REASONING_BUDGET:-32768}" \
  --jinja \
  ${LOAD_ARGS}

# --reasoning-budget caps tokens spent inside <think>...</think> per response.
# Llama.cpp's grammar engine is meant to force-close the block when this is
# hit, but enforcement depends on the model producing a recognizable
# end-of-thinking token. When that doesn't happen, --n-predict above is the
# unconditional ceiling that still fires. Hoisted out of LLAMACPP_EXTRA_ARGS
# so operators have one canonical knob exposed in .env.

# --n-predict is a hard ceiling on tokens generated per request, independent
# of --reasoning-budget. Reasoning-budget tracks tokens inside the model's
# <think>...</think> block and only kicks in if the model emits the closing
# tag — when the model gets confused in a large context (e.g. 248K input
# tokens after a tool-spam loop), it can produce reasoning tokens forever
# without emitting </think>, defeating the budget. The n-predict cap fires
# regardless and force-terminates with finish_reason=length. Tuned high
# enough (~64K) that normal responses are unaffected.

# A MoE model placed partly in system RAM (catalog n_cpu_moe): the expert tensors of its first N layers
# stay on the CPU. The placement is explicit, so llama.cpp's own --fit must not re-place it.
if [ -n "${LLAMACPP_N_CPU_MOE:-}" ]; then
  set -- "$@" --n-cpu-moe "${LLAMACPP_N_CPU_MOE}" --fit off
fi

# The CPU threads llama.cpp computes with (catalog cpu_threads): the CPU load offloaded experts add.
if [ -n "${LLAMACPP_THREADS:-}" ]; then
  set -- "$@" --threads "${LLAMACPP_THREADS}"
fi

if [ -n "${LLAMACPP_OVERRIDE_KV:-}" ]; then
  set -- "$@" --override-kv "${LLAMACPP_OVERRIDE_KV}"
fi

if [ -n "${LLAMACPP_MMPROJ:-}" ]; then
  if [ ! -f "${LLAMACPP_MMPROJ}" ]; then
    echo "warning: LLAMACPP_MMPROJ=${LLAMACPP_MMPROJ} not found; vision disabled"
  else
    set -- "$@" --mmproj "${LLAMACPP_MMPROJ}"
  fi
fi

if [ "${LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION:-0}" = "1" ]; then
  set -- "$@" \
    --cache-type-k "${LLAMACPP_KV_CACHE_TYPE_K:-q4_0}" \
    --cache-type-v "${LLAMACPP_KV_CACHE_TYPE_V:-q4_0}"
fi

if [ -n "${LLAMACPP_EXTRA_ARGS:-}" ]; then
  # Intentionally split LLAMACPP_EXTRA_ARGS on whitespace so operators can append
  # raw llama-server flags from .env without changing compose.
  # shellcheck disable=SC2086
  set -- "$@" ${LLAMACPP_EXTRA_ARGS}
fi

# Re-append the original container CMD (captured above, e.g. --metrics) LAST so it
# survives the `set --` reset and llama-server's last-wins parsing honors it.
if [ -n "${LLAMACPP_CMD_ARGS}" ]; then
  # shellcheck disable=SC2086
  set -- "$@" ${LLAMACPP_CMD_ARGS}
fi

echo "llama-server args: $*"
exec /app/llama-server "$@"
