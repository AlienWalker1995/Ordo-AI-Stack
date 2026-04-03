#!/bin/sh
# Bind-mounted ComfyUI models are often root-owned; dashboard runs as appuser (1000).
# Match comfyui-model-puller: ensure bind mounts are accessible, then drop privileges.
set -e
mkdir -p /models/checkpoints /models/unet /models/loras /models/text_encoders \
  /models/latent_upscale_models /models/vae /models/diffusion_models /models/vae_approx
if ! gosu appuser sh -c "test -w /models" 2>/dev/null; then
  chmod -R a+w /models 2>/dev/null || true
fi

# OpenClaw config is mounted from the host and may be root-owned or too restrictive
# for appuser on some Docker Desktop / bind-mount combinations.
if [ -d /openclaw-config ]; then
  if ! gosu appuser sh -c "test -r /openclaw-config" 2>/dev/null; then
    chmod a+rx /openclaw-config 2>/dev/null || true
  fi
  if [ -f /openclaw-config/openclaw.json ] && ! gosu appuser sh -c "test -r /openclaw-config/openclaw.json" 2>/dev/null; then
    chmod a+r /openclaw-config/openclaw.json 2>/dev/null || true
  fi
fi

exec gosu appuser "$@"
