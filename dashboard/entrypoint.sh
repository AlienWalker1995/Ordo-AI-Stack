#!/bin/sh
# Bind-mounted ComfyUI models are often root-owned; dashboard runs as appuser (1000).
# Match comfyui-model-puller: ensure bind mounts are accessible, then drop privileges.
set -e
mkdir -p /models/checkpoints /models/unet /models/loras /models/text_encoders \
  /models/latent_upscale_models /models/vae /models/diffusion_models /models/vae_approx
if ! gosu appuser sh -c "test -w /models" 2>/dev/null; then
  chmod -R a+w /models 2>/dev/null || true
fi

exec gosu appuser "$@"
