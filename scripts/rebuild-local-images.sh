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
#
# The image -> build-context map comes from `ordo.buildspec`, which is the SAME resolver that
# `ordo preflight` and tests/substrate/test_build_contexts.py use. An earlier version of this
# script carried its own hardcoded list and passed the repo root as the context for everything;
# only ops-controller actually builds that way, so a rename here could silently diverge from the
# manifests. Reading the resolver means a new service is covered the moment it declares a build.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-latest}"
FAILED=()

plan() {
  python - <<'PY'
import sys
from pathlib import Path

from ordo import buildspec

# This script runs under Git Bash on Windows, where python's text stdout translates "\n" to
# "\r\n". `read` then leaves the CR on the last field, and every build fails with
# `path "services/hermes\r" not found`. Write LF regardless of platform.
sys.stdout.reconfigure(newline="\n")
from ordo.agents import AgentRegistry
from ordo.dashboards import DashboardRegistry
from ordo.plugins import PluginRegistry

root = Path(".")
services = root / "services"
contexts = buildspec.manifest_image_contexts(
    PluginRegistry.load(services),
    AgentRegistry.load(services),
    DashboardRegistry.load(services),
    project="ordo",
)
# The substrate services have no manifest: their contexts are declared in ordo.compose.
for name, ctx in buildspec.SUBSTRATE_BUILD_CONTEXTS.items():
    contexts.setdefault(f"ordo/{name}", ctx)

# Deliberately NOT built here:
#   llamacpp-patched  a pinned upstream patch build, rebuilt only on a llama.cpp bump
#   ltx-trainer       the LTX models were deleted 2026-09-20; nothing runs it
SKIP = {"ordo/llamacpp-patched", "ordo/ltx-trainer"}

for image, ctx in sorted(contexts.items()):
    if image in SKIP or ctx == "<external>":
        continue
    dockerfile = f"{ctx}/Dockerfile"
    if not (root / dockerfile).is_file():
        print(f"# no Dockerfile at {dockerfile} for {image}")
        continue
    # ops-controller ships the substrate package (ordo/, catalog/, services/), so it is the one
    # image whose context is the repo root; the root .dockerignore allowlist keeps it tiny.
    build_ctx = "." if image == "ordo/ops-controller" else ctx
    print(f"{image}\t{dockerfile}\t{build_ctx}")
PY
}

while IFS=$'\t' read -r image dockerfile context; do
  case "$image" in \#*) printf '%s\n' "$image"; continue ;; esac
  [ -n "$image" ] || continue
  printf '\n=== %s:%s  (-f %s, context %s)\n' "$image" "$TAG" "$dockerfile" "$context"
  if docker build -f "$dockerfile" -t "$image:$TAG" "$context"; then
    echo "OK   $image:$TAG"
  else
    echo "FAIL $image:$TAG"
    FAILED+=("$image")
  fi
done < <(plan)

printf '\n===============================\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "all images built at :$TAG"
else
  printf 'FAILED (%d): %s\n' "${#FAILED[@]}" "${FAILED[*]}"
  exit 1
fi
