#!/usr/bin/env bash
# Install Python requirements for a custom_nodes subfolder into the running comfyui container.
# Prefer: POST $DASHBOARD_URL/api/comfyui/install-node-requirements through the Caddy edge (oauth2-proxy/Google SSO) — no dashboard token needed — see docs/runbooks/TROUBLESHOOTING.md
# Usage (from repo root): ./scripts/comfyui/install_node_requirements.sh "MyNodePack"
# Requires: docker compose, comfyui service up; BASE_PATH optional (defaults to repo root).
set -euo pipefail

# On Windows Git Bash, MSYS mangles absolute container paths (e.g. /root/ComfyUI/...) passed
# through to docker.exe into host paths before they reach the container. No-op elsewhere.
export MSYS_NO_PATHCONV=1

NODE_PATH="${1:?Usage: $0 <path-under-custom_nodes> e.g. Juno-ComfyUI or juno-comfyui-nodes-main}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${BASE_PATH:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
BASE="${BASE//\\/\/}"

# Normalize for container (always forward slashes)
NODE_PATH_POSIX="${NODE_PATH//\\//}"

REQ="$BASE/data/comfyui-storage/ComfyUI/custom_nodes/$NODE_PATH_POSIX/requirements.txt"
if [[ ! -f "$REQ" ]]; then
  echo "Missing requirements file: $REQ" >&2
  exit 1
fi

cd "$BASE"
exec docker compose --project-directory out -f out/docker-compose.yml -p ordo exec comfyui \
  python3 -m pip install -r "/root/ComfyUI/custom_nodes/$NODE_PATH_POSIX/requirements.txt"
