#!/usr/bin/env python3
"""Config-driven ComfyUI model downloader — direct streaming to destination, no cache.

Downloads HuggingFace models directly to the ComfyUI model directories.
Uses stdlib urllib only; no external dependencies, no intermediate cache.
Resumes interrupted downloads via HTTP Range requests.

Environment variables:
  MODELS_DIR        Target ComfyUI models root (default: /models)
  COMFYUI_PACKS     Comma-separated pack names to download (default: from models.json defaults)
  COMFYUI_QUANT     GGUF quantization level for {quant} templates (default: Q4_K_M)
  COMFYUI_CONFIG    Path to models.json override (default: <script_dir>/models.json)
  HF_TOKEN          HuggingFace token for gated/private repos (optional)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
QUANT = os.environ.get("COMFYUI_QUANT", "Q4_K_M")
CONFIG_PATH = Path(os.environ.get("COMFYUI_CONFIG", SCRIPT_DIR / "models.json"))
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""

ALL_SUBDIRS = ("unet", "checkpoints", "text_encoders", "loras", "latent_upscale_models", "vae")
CHUNK = 16 * 1024 * 1024  # 16 MB read chunks


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop Authorization header when redirected off huggingface.co (CDN uses pre-signed URLs)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req and "huggingface.co" not in newurl:
            for key in list(new_req.headers):
                if key.lower() == "authorization":
                    del new_req.headers[key]
        return new_req


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def load_config():
    if not CONFIG_PATH.exists():
        print(f"ERROR: Config not found: {CONFIG_PATH}", flush=True)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def resolve_packs(config):
    packs_env = os.environ.get("COMFYUI_PACKS", "").strip()
    if packs_env:
        requested = [p.strip() for p in packs_env.split(",") if p.strip()]
        if requested == ["all"]:
            return list(config["packs"].keys())
        unknown = [p for p in requested if p not in config["packs"]]
        if unknown:
            available = ", ".join(config["packs"].keys())
            print(f"ERROR: Unknown packs: {', '.join(unknown)}", flush=True)
            print(f"Available: {available}", flush=True)
            sys.exit(1)
        return requested
    return config.get("defaults", {}).get("packs", list(config["packs"].keys()))


def download_model(repo_id: str, filename: str, subdir: str, dest_name: str | None = None, url: str | None = None) -> bool:
    filename = filename.format(quant=QUANT)

    # If full URL provided, parse it to extract repo and filename
    if url:
        try:
            # Parse: https://huggingface.co/{repo_id}/resolve/main/{file_path}
            url_parts = url.replace("https://huggingface.co/", "").replace("http://huggingface.co/", "").split("/")
            if len(url_parts) >= 3:
                parsed_repo = "/".join(url_parts[:2])
                parsed_file = "/".join(url_parts[3:])  # Skip "resolve"
                if repo_id == parsed_repo or not repo_id:
                    repo_id = parsed_repo
                if filename == parsed_file or not filename:
                    filename = parsed_file
        except Exception as e:
            print(f"  Warning: Could not parse URL: {e}", flush=True)
    dest_name = dest_name or Path(filename).name
    dest_dir = MODELS_DIR / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / dest_name

    if dest_path.exists() and dest_path.stat().st_size > 0:
        size_mb = dest_path.stat().st_size // (1024 * 1024)
        print(f"  OK (exists): {subdir}/{dest_name} ({size_mb} MB)", flush=True)
        return True

    url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
    part_path = dest_dir / (dest_name + ".part")
    resume_from = part_path.stat().st_size if part_path.exists() else 0

    print(f"  Downloading: {dest_name} (from {repo_id})", flush=True)
    if resume_from:
        print(f"  Resuming from {resume_from // (1024 * 1024)} MB", flush=True)

    try:
        headers: dict[str, str] = {"User-Agent": "comfyui-model-puller/2.0"}
        if HF_TOKEN:
            headers["Authorization"] = f"Bearer {HF_TOKEN}"
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        req = urllib.request.Request(url, headers=headers)
        with _opener.open(req) as resp:
            status = resp.status
            content_length = int(resp.headers.get("Content-Length") or 0)
            # Total = content remaining + already downloaded (if server honored Range)
            total = content_length + (resume_from if status == 206 else 0)
            downloaded = resume_from if status == 206 else 0

            mode = "ab" if (status == 206 and resume_from) else "wb"
            with open(part_path, mode) as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        mb = downloaded // (1024 * 1024)
                        total_mb = total // (1024 * 1024)
                        print(f"\r  {pct}% — {mb}/{total_mb} MB", end="", flush=True)

        print(flush=True)
        part_path.rename(dest_path)
        size_mb = dest_path.stat().st_size // (1024 * 1024)
        print(f"  Done: {subdir}/{dest_name} ({size_mb} MB)", flush=True)
        return True

    except urllib.error.HTTPError as e:
        print(f"\n  ERROR {e.code} {e.reason}: {dest_name}", flush=True)
        return False
    except Exception as e:
        print(f"\n  ERROR: {dest_name}: {e}", flush=True)
        return False


def main() -> int:
    config = load_config()
    global QUANT
    QUANT = os.environ.get("COMFYUI_QUANT") or config.get("defaults", {}).get("quant", "Q4_K_M")

    pack_names = resolve_packs(config)
    packs = config["packs"]

    models = []
    for pack_name in pack_names:
        for m in packs[pack_name]["models"]:
            models.append((pack_name, m))

    print(f"Packs: {', '.join(pack_names)} ({len(models)} models, quant={QUANT})", flush=True)
    print(f"Target: {MODELS_DIR}", flush=True)

    for sub in ALL_SUBDIRS:
        (MODELS_DIR / sub).mkdir(parents=True, exist_ok=True)

    ok = True
    for i, (pack_name, m) in enumerate(models, 1):
        print(f"[{i}/{len(models)}] {pack_name}:", flush=True)
        if not download_model(
            m["repo"],
            m["file"],
            m["dest"],
            m.get("name"),
            m.get("url")
        ):
            ok = False

    if ok:
        print(f"All {len(models)} ComfyUI models ready.", flush=True)
    else:
        print("Some downloads failed. Re-run to retry.", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
