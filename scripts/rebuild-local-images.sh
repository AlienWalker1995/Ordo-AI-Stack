#!/usr/bin/env bash
# Rebuild every locally built image from the current checkout.
#
# out/docker-compose.yml is image-only by design: `docker compose build` there is a silent no-op,
# so each image is built out of band from its own Dockerfile. Tags are passed in rather than
# hardcoded to :latest so a branch build never overwrites what the stack is running.
#
#   ./scripts/rebuild-local-images.sh            # build to :latest (promotes immediately)
#   ./scripts/rebuild-local-images.sh mybranch   # build to :mybranch (safe, promotes nothing)
#
# It does NOT recreate containers. Promotion is a separate, deliberate step.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-latest}"
FAILED=()

build() {  # build <image-name> <dockerfile-dir> [context]
  local image="$1" dir="$2" ctx="${3:-.}"
  printf '\n=== %s:%s  (from services/%s)\n' "$image" "$TAG" "$dir"
  if docker build -f "services/$dir/Dockerfile" -t "$image:$TAG" "$ctx"; then
    echo "OK   $image:$TAG"
  else
    echo "FAIL $image:$TAG"
    FAILED+=("$image")
  fi
}

build ordo/agent-hermes          hermes
build ordo/codebase-memory-mcp   codebase-memory
build ordo/codebase-memory-ui    codebase-memory-ui
build ordo/comfyui-mcp           comfyui-mcp
build ordo/evals                 evals
build ordo/gpu-gate              gpu-gate
build ordo/livesync-bridge       obsidian-livesync
build ordo/mcpvault-mcp          memory-vault
build ordo/model-gateway         model-gateway
build ordo/n8n-mcp               n8n
build ordo/ops-api               ops-api
build ordo/ops-controller        ops-controller
build ordo/orchestration-mcp     orchestration
build ordo/qdrant-rag-mcp        qdrant-rag
build ordo/rag-ingestion         rag
build ordo/dashboard-v1          v1-parity/dashboard

# Deliberately NOT built here:
#   ordo-ai-stack-llamacpp-patched  - a pinned upstream patch build, rebuilt only on a llama.cpp bump
#   ordo/ltx-trainer                - the LTX models were deleted 2026-09-20; nothing runs it

printf '\n===============================\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "all images built at :$TAG"
else
  printf 'FAILED (%d): %s\n' "${#FAILED[@]}" "${FAILED[*]}"
  exit 1
fi
