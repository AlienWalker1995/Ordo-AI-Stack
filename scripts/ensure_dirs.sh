#!/usr/bin/env bash
# Creates data directories for bind mounts. Run before first docker compose up.
# Linux/Mac: ./scripts/ensure_dirs.sh

set -e

base="${BASE_PATH:-$(pwd)}"
base="${base//\\/\/}"
data="${DATA_PATH:-$base/data}"

dirs=(
  "$base/models/gguf"
  "$data/mcp"
  "$data/ops-controller"
  "$data/open-webui"
  "$data/comfyui-storage"
  "$data/comfyui-storage/ComfyUI/custom_nodes"
  "$data/comfyui-storage/ComfyUI/user/__manager"
  "$data/comfyui-output"
  "$data/comfyui-storage/ComfyUI/user/default/workflows"
  "$data/comfyui-storage/ComfyUI/user/default/workflows/mcp-api"
  "$data/n8n-data"
  "$data/n8n-files"
  "$data/dashboard"
  "$data/qdrant"
  "$data/openclaude"
  "$base/models/comfyui/checkpoints"
  "$base/models/comfyui/loras"
  "$base/models/comfyui/latent_upscale_models"
  "$base/models/comfyui/text_encoders"
)

for d in "${dirs[@]}"; do
  mkdir -p "$d"
  echo "OK $d"
done

# ComfyUI-Manager: seed security_level=weak so git/pip installs work when ComfyUI uses --listen (Docker)
manager_seed="$base/config/comfyui-manager-seed.ini"
manager_cfg="$data/comfyui-storage/ComfyUI/user/__manager/config.ini"
if [[ -f "$manager_seed" ]] && [[ ! -f "$manager_cfg" ]]; then
  cp "$manager_seed" "$manager_cfg"
  echo "OK $manager_cfg (ComfyUI-Manager security_level=weak)"
fi

# Seed ComfyUI user workflows (data/ is gitignored). API graphs under mcp-api/ (default COMFY_MCP_DEFAULT_WORKFLOW_ID=mcp-api/generate_image).
wf_template="$base/workflow-templates/comfyui-workflows"
wf_mcp_api="$data/comfyui-storage/ComfyUI/user/default/workflows/mcp-api"
legacy_wf="$data/comfyui-workflows"
if [[ -d "$legacy_wf" ]]; then
  for f in "$legacy_wf"/*.json; do
    [[ -f "$f" ]] || continue
    fname=$(basename "$f")
    if [[ ! -f "$wf_mcp_api/$fname" ]]; then
      cp "$f" "$wf_mcp_api/$fname"
      echo "OK migrate legacy comfyui-workflows/$fname -> .../workflows/mcp-api/"
    fi
  done
fi
if [[ -d "$wf_template" ]]; then
  for f in "$wf_template"/*.json; do
    [[ -f "$f" ]] || continue
    fname=$(basename "$f")
    if [[ ! -f "$wf_mcp_api/$fname" ]]; then
      cp "$f" "$wf_mcp_api/$fname"
      echo "OK bootstrap workflows/mcp-api/$fname"
    fi
  done
fi

# Bootstrap MCP servers.txt with default tools (gateway hot-reloads)
mcp_servers="$data/mcp/servers.txt"
mcp_registry="$data/mcp/registry-custom.yaml"
if [[ ! -f "$mcp_servers" ]]; then
  echo "duckduckgo,n8n,tavily,comfyui" > "$mcp_servers"
  echo "OK $mcp_servers (duckduckgo,n8n,tavily,comfyui)"
fi
# Bootstrap catalog fragment for ComfyUI (gateway uses --additional-catalog)
if [[ ! -f "$mcp_registry" ]] && [[ -f "$base/mcp/gateway/registry-custom.yaml" ]]; then
  cp "$base/mcp/gateway/registry-custom.yaml" "$mcp_registry"
  echo "OK $mcp_registry"
fi

# Fix ownership for non-root dashboard (PRD section 5): models and data must be writable by uid 1000
if command -v chown >/dev/null 2>&1; then
  chown -R 1000:1000 "$base/models/comfyui" "$data" 2>/dev/null && echo "OK chown models+data (dashboard non-root)" || true
fi

# Auto-detect GPU and generate overrides/compute.yml
detect_script="$base/scripts/detect_hardware.py"
if [[ -f "$detect_script" ]]; then
  BASE_PATH="$base" python3 "$detect_script" 2>/dev/null && echo "OK Hardware detected (overrides/compute.yml)"
fi

# SSRF egress block: run after first 'docker compose up' to block cloud metadata access from MCP containers.
# See docs/runbooks/SECURITY_HARDENING.md
if [[ -f "$base/scripts/ssrf-egress-block.sh" ]] && command -v iptables >/dev/null 2>&1; then
  echo "Note: After first 'docker compose up', run: sudo ./scripts/ssrf-egress-block.sh (blocks SSRF from MCP)"
fi

# Configure OpenClaude on the host to use the local OpenAI-compatible model-gateway.
port="${MODEL_GATEWAY_PORT:-11435}"
openclaude_model=""
if [[ -f "$root_env" ]]; then
  openclaude_model=$(grep -E '^OPENCLAUDE_MODEL=' "$root_env" 2>/dev/null | tail -n 1 | cut -d= -f2-)
  if [[ -z "$openclaude_model" ]]; then
    openclaude_model=$(grep -E '^DEFAULT_MODEL=' "$root_env" 2>/dev/null | tail -n 1 | cut -d= -f2-)
  fi
fi
if command -v openclaude >/dev/null 2>&1; then
  configured=false
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] || continue
    if ! grep -q 'OPENCLAUDE_LOCAL_MODEL_GATEWAY' "$rc"; then
      {
        printf '\n# OPENCLAUDE_LOCAL_MODEL_GATEWAY\n'
        printf 'export CLAUDE_CODE_USE_OPENAI=1\n'
        printf 'export OPENAI_API_KEY=local\n'
        printf 'export OPENAI_BASE_URL=http://localhost:%s/v1\n' "$port"
        if [[ -n "$openclaude_model" ]]; then
          printf 'export OPENAI_MODEL=%s\n' "$openclaude_model"
        fi
      } >> "$rc"
      configured=true
    fi
  done
  if grep -q 'export ANTHROPIC_API_KEY=local' "$HOME/.bashrc" "$HOME/.zshrc" 2>/dev/null; then
    echo "Note: old local ANTHROPIC_* exports may still exist in your shell rc from the Claude Code flow."
  fi
  if [[ "$configured" == "true" ]]; then
    echo "OK OpenClaude configured -> http://localhost:$port/v1 (run: source ~/.bashrc)"
  else
    echo "OK OpenClaude already configured in shell rc"
  fi
  if [[ -n "$openclaude_model" ]]; then
    echo "   Default model: $openclaude_model"
  fi
  echo "   Usage: openclaude"
else
  echo "Note: OpenClaude not installed. To install:"
  echo "        npm install -g @gitlawb/openclaude"
  echo "      Or use the Dockerized OpenClaude CLI:"
  echo "        docker compose --profile openclaude-cli run --rm openclaude-cli"
  echo "      Then re-run this script to configure host OpenClaude automatically."
fi

echo "Directories ready."
