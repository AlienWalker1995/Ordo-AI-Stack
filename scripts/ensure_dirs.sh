#!/usr/bin/env bash
# Creates data directories for bind mounts. Run before first docker compose up.
# Linux/Mac: ./scripts/ensure_dirs.sh

set -e

base="${BASE_PATH:-$(pwd)}"
base="${base//\\/\/}"
data="${DATA_PATH:-$base/data}"

dirs=(
  "$data/ollama"
  "$data/mcp"
  "$data/open-webui"
  "$data/comfyui-storage"
  "$data/comfyui-output"
  "$data/n8n-data"
  "$data/n8n-files"
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

# Ensure openclaw/.env exists with a valid token (required for OpenClaw service)
openclaw_env="$base/openclaw/.env"
openclaw_example="$base/openclaw/.env.example"
needs_create=false
needs_token=false

if [[ ! -f "$openclaw_env" ]]; then
  needs_create=true
elif grep -q 'change-me-to-a-long-random-token' "$openclaw_env" 2>/dev/null; then
  needs_token=true
fi

if [[ "$needs_create" == "true" || "$needs_token" == "true" ]]; then
  token=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p -c 32)
  if [[ -f "$openclaw_example" ]]; then
    while IFS= read -r line; do
      if [[ "$line" =~ ^BASE_PATH= ]]; then
        echo "BASE_PATH=$base"
      elif [[ "$line" =~ change-me-to-a-long-random-token ]]; then
        echo "OPENCLAW_GATEWAY_TOKEN=$token"
      else
        echo "$line"
      fi
    done < "$openclaw_example" > "$openclaw_env.tmp"
    mv "$openclaw_env.tmp" "$openclaw_env"
  else
    echo -e "BASE_PATH=$base\nOPENCLAW_GATEWAY_TOKEN=$token" > "$openclaw_env"
  fi
  echo "OK openclaw/.env ($([[ "$needs_create" == "true" ]] && echo 'created' || echo 'token fixed'))"
fi

# Auto-detect GPU and generate docker-compose.compute.yml
detect_script="$base/scripts/detect_hardware.py"
if [[ -f "$detect_script" ]]; then
  BASE_PATH="$base" python3 "$detect_script" 2>/dev/null && echo "OK Hardware detected (docker-compose.compute.yml)"
fi

echo "Directories ready."
