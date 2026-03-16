#!/usr/bin/env python3
"""Download LTX-2.3 models for ComfyUI from Hugging Face.

Uses symlinks to the HF cache to avoid duplicating large files on disk.
Based on: https://huggingface.co/unsloth/LTX-2.3-GGUF

Model choices:
  Q4_K_M — good quality/size balance (~12 GB for main model).
  Override with COMFYUI_QUANT env var (e.g. Q8_0 for near-lossless).
"""
import os
import sys

MODELS_DIR = os.environ.get("MODELS_DIR", "/models")
QUANT = os.environ.get("COMFYUI_QUANT", "Q4_K_M")

# (repo_id, filename_template, dest_subdir, [dest_name])
# {quant} is replaced with COMFYUI_QUANT env var.
DOWNLOADS = [
    # --- LTX-2.3 dev model (main unet/diffusion model) ---
    ("unsloth/LTX-2.3-GGUF", "ltx-2.3-22b-dev-{quant}.gguf", "unet", None),

    # --- VAE ---
    ("unsloth/LTX-2.3-GGUF", "vae/ltx-2.3-22b-dev_video_vae.safetensors", "vae", None),
    ("unsloth/LTX-2.3-GGUF", "vae/ltx-2.3-22b-dev_audio_vae.safetensors", "vae", None),

    # --- Embeddings connector ---
    ("unsloth/LTX-2.3-GGUF", "text_encoders/ltx-2.3-22b-dev_embeddings_connectors.safetensors",
     "text_encoders", None),

    # --- Distilled LoRA (fast draft/preview generation) ---
    ("Lightricks/LTX-2.3", "ltx-2.3-22b-distilled-lora-384.safetensors", "loras", None),

    # --- Spatial upscaler (2x) ---
    ("Lightricks/LTX-2.3", "ltx-2.3-spatial-upscaler-x2-1.0.safetensors",
     "latent_upscale_models", None),

    # --- Gemma 3 12B text encoder (QAT quantized, ~8 GB) ---
    ("unsloth/gemma-3-12b-it-qat-GGUF", "gemma-3-12b-it-qat-UD-Q4_K_XL.gguf",
     "text_encoders", None),

    # --- Gemma 3 multimodal projector ---
    ("unsloth/gemma-3-12b-it-qat-GGUF", "mmproj-BF16.gguf", "text_encoders", None),
]

SUBDIRS = ("unet", "checkpoints", "text_encoders", "loras", "latent_upscale_models", "vae")


def ensure_huggingface_hub():
    try:
        from huggingface_hub import hf_hub_download  # noqa: F401
    except ImportError:
        print("Installing huggingface_hub...", flush=True)
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])
        print("huggingface_hub installed.", flush=True)


def download(repo_id, filename, subdir, dest_name=None):
    from huggingface_hub import hf_hub_download

    # Resolve quant placeholder
    filename = filename.format(quant=QUANT)
    dest_name = dest_name or os.path.basename(filename)
    dest_path = os.path.join(MODELS_DIR, subdir, dest_name)

    if os.path.exists(dest_path):
        print(f"==> OK (exists): {subdir}/{dest_name}", flush=True)
        return True

    print(f"==> Downloading: {dest_name} (from {repo_id})", flush=True)
    try:
        cached = hf_hub_download(repo_id=repo_id, filename=filename)
        # Symlink to HF cache to avoid duplicating large files on disk.
        # Fall back to copy if symlinks aren't supported (e.g. some Windows/network mounts).
        try:
            os.symlink(cached, dest_path)
            print(f"==> Linked: {subdir}/{dest_name}", flush=True)
        except OSError:
            import shutil
            shutil.copy2(cached, dest_path)
            print(f"==> Copied: {subdir}/{dest_name}", flush=True)
        return True
    except Exception as e:
        print(f"ERROR: {dest_name}: {e}", flush=True)
        return False


def main():
    print(f"Setting up model directories (quant={QUANT})...", flush=True)
    for sub in SUBDIRS:
        os.makedirs(os.path.join(MODELS_DIR, sub), exist_ok=True)

    ensure_huggingface_hub()

    total = len(DOWNLOADS)
    ok = True
    for i, (repo_id, filename, subdir, dest_name) in enumerate(DOWNLOADS, 1):
        print(f"--- [{i}/{total}] ---", flush=True)
        if not download(repo_id, filename, subdir, dest_name):
            ok = False

    if ok:
        print("All LTX-2.3 ComfyUI models ready.", flush=True)
    else:
        print("Some downloads failed. Re-run to retry.", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
