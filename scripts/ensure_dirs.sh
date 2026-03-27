#!/usr/bin/env bash
# Creates data directories for bind mounts. Run before first docker compose up.
# Linux/Mac: ./scripts/ensure_dirs.sh

set -e

base="${BASE_PATH:-$(pwd)}"
base="${base//\\/\/}"
data="${DATA_PATH:-$base/data}"

dirs=(
  "$base/models/ollama"
  "$data/mcp"
  "$data/ops-controller"
  "$data/open-webui"
  "$data/comfyui-storage"
  "$data/comfyui-storage/ComfyUI/custom_nodes"
  "$data/comfyui-storage/ComfyUI/user/__manager"
  "$data/comfyui-output"
  "$data/comfyui-workflows"
  "$data/comfyui-storage/ComfyUI/user/default/workflows"
  "$data/n8n-data"
  "$data/n8n-files"
  "$data/dashboard"
  "$data/qdrant"
  "$data/openclaw"
  "$data/openclaw/workspace"
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

# Seed data/comfyui-workflows from repo templates (data/ is gitignored; COMFY_MCP_DEFAULT_WORKFLOW_ID defaults to generate_image)
wf_template="$base/workflow-templates/comfyui-workflows"
wf_data="$data/comfyui-workflows"
if [[ -d "$wf_template" ]]; then
  for f in "$wf_template"/*.json; do
    [[ -f "$f" ]] || continue
    fname=$(basename "$f")
    if [[ ! -f "$wf_data/$fname" ]]; then
      cp "$f" "$wf_data/$fname"
      echo "OK bootstrap comfyui-workflows/$fname"
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

# Fix ownership for non-root dashboard (PRD §5): models and data must be writable by uid 1000
if command -v chown >/dev/null 2>&1; then
  chown -R 1000:1000 "$base/models/comfyui" "$data" 2>/dev/null && echo "OK chown models+data (dashboard non-root)" || true
fi

# Bootstrap openclaw.json with Ollama provider if config doesn't exist
openclaw_config="$data/openclaw/openclaw.json"
openclaw_config_example="$base/openclaw/openclaw.json.example"
if [[ ! -f "$openclaw_config" && -f "$openclaw_config_example" ]]; then
  cp "$openclaw_config_example" "$openclaw_config"
  echo "OK openclaw config (Ollama provider)"
fi

# Ensure root .env has OPENCLAW_GATEWAY_TOKEN (required for OpenClaw service)
root_env="$base/.env"
root_example="$base/.env.example"
needs_create=false
needs_token=false

if [[ ! -f "$root_env" ]]; then
  needs_create=true
elif grep -q 'OPENCLAW_GATEWAY_TOKEN=change-me\|^OPENCLAW_GATEWAY_TOKEN=[[:space:]]*$' "$root_env" 2>/dev/null; then
  needs_token=true
elif ! grep -q '^OPENCLAW_GATEWAY_TOKEN=[a-zA-Z0-9]' "$root_env" 2>/dev/null; then
  needs_token=true
fi

if [[ "$needs_create" == "true" || "$needs_token" == "true" ]]; then
  token=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p -c 32)
  if [[ "$needs_create" == "true" && -f "$root_example" ]]; then
    cp "$root_example" "$root_env"
  fi
  if [[ -f "$root_env" ]]; then
    if grep -q 'OPENCLAW_GATEWAY_TOKEN=' "$root_env" 2>/dev/null; then
      sed -i.bak "s/^OPENCLAW_GATEWAY_TOKEN=.*/OPENCLAW_GATEWAY_TOKEN=$token/" "$root_env" && rm -f "$root_env.bak"
    else
      echo "" >> "$root_env"
      echo "# OpenClaw gateway auth (pinned; do not change unless re-pairing all devices)" >> "$root_env"
      echo "OPENCLAW_GATEWAY_TOKEN=$token" >> "$root_env"
    fi
  else
    echo -e "BASE_PATH=$base\nOPENCLAW_GATEWAY_TOKEN=$token" > "$root_env"
  fi
  echo "OK .env ($([[ "$needs_create" == "true" ]] && echo 'created' || echo 'OPENCLAW_GATEWAY_TOKEN set'))"
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

# Configure Claude Code to route through the local model-gateway (optional; dashboard toggle: data/dashboard/claude_code_env_overwrite.json)
claude_env_json="$data/dashboard/claude_code_env_overwrite.json"
claude_env_overwrite=true
if [[ -f "$claude_env_json" ]] && command -v python3 >/dev/null 2>&1; then
  en=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get('enabled',True) else 0)" "$claude_env_json" 2>/dev/null || echo 1)
  [[ "$en" == "0" ]] && claude_env_overwrite=false
fi
if command -v claude >/dev/null 2>&1; then
  port="${MODEL_GATEWAY_PORT:-11435}"
  if [[ "$claude_env_overwrite" == "true" ]]; then
    configured=false
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [[ -f "$rc" ]] && ! grep -q 'ANTHROPIC_BASE_URL' "$rc"; then
        printf '\n# Claude Code — local model-gateway\nexport ANTHROPIC_API_KEY=local\nexport ANTHROPIC_BASE_URL=http://localhost:%s\n' "$port" >> "$rc"
        configured=true
      fi
    done
    if [[ "$configured" == "true" ]]; then
      echo "OK Claude Code configured -> http://localhost:$port (run: source ~/.bashrc)"
    else
      echo "OK Claude Code already configured in shell rc"
    fi
    echo "   Usage: claude --model <ollama-model-name>"
  else
    echo "Claude Code: local Model Gateway routing disabled (data/dashboard/claude_code_env_overwrite.json). Remove ANTHROPIC_* from ~/.bashrc or ~/.zshrc if you no longer want local routing."
  fi
else
  echo "Note: Claude Code not installed. To install:"
  echo "        npm install -g @anthropic-ai/claude-code"
  echo "      Then re-run this script to configure it automatically."
fi

echo "Directories ready."
