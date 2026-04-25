#!/usr/bin/env python3
"""Download GGUF files into /models (bind-mounted models/gguf).

Env:
  GGUF_MODELS — comma-separated entries. Each entry is either:
    - HuggingFace repo id, e.g. bartowski/Llama-3.2-3B-Instruct-GGUF
    - huggingface.co URL to a single .gguf file
  HF_TOKEN — optional, for gated repos. May also be supplied via
    HF_TOKEN_FILE pointing at a Docker secret (file wins if both set).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _read_token(env_name: str) -> str | None:
    """Resolve a token from {NAME}_FILE (Docker secrets) or {NAME} (env)."""
    file_var = f"{env_name}_FILE"
    path = os.environ.get(file_var)
    if path:
        try:
            value = open(path).read().strip()
            if value:
                return value
        except OSError:
            pass
    value = os.environ.get(env_name, "").strip()
    return value or None


def _install_hf_hub() -> None:
    import subprocess

    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "huggingface_hub>=0.20.0"],
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    raw = os.environ.get("GGUF_MODELS", "").strip()
    if not raw:
        print("GGUF_MODELS empty; nothing to pull.")
        return 0

    dest = Path(os.environ.get("GGUF_DEST", "/models"))
    dest.mkdir(parents=True, exist_ok=True)
    token = _read_token("HF_TOKEN")

    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("Installing huggingface_hub …")
        _install_hf_hub()

    from huggingface_hub import hf_hub_download, list_repo_files

    entries = [x.strip() for x in raw.split(",") if x.strip()]
    for entry in entries:
        print(f"==> {entry}")
        if entry.startswith("http://") or entry.startswith("https://"):
            m = re.search(r"huggingface\.co/([^/]+/[^/]+)/(?:blob/main|resolve/main)/(.+\.gguf)", entry)
            if not m:
                print("    Skip: unsupported URL (need huggingface.co …/resolve/main/file.gguf)")
                continue
            repo_id, filename = m.group(1), m.group(2)
            path = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(dest), local_dir_use_symlinks=False, token=token)
            print(f"    -> {path}")
            continue

        # Repo id: bare (owner/repo) or with quant filter (owner/repo:UD-Q2_K_XL)
        quant_filter = None
        repo_id = entry
        if ":" in entry:
            repo_id, quant_filter = entry.rsplit(":", 1)

        try:
            files = list_repo_files(repo_id, token=token)
        except Exception as e:
            print(f"    Error listing {repo_id}: {e}")
            return 1
        ggufs = [f for f in files if f.endswith(".gguf") and "/" not in f]
        if not ggufs:
            print(f"    No root-level .gguf in {repo_id}; add a direct file URL or adjust repo.")
            continue

        if quant_filter:
            matches = [g for g in ggufs if quant_filter.lower() in g.lower()]
            if not matches:
                print(f"    No .gguf matching '{quant_filter}' in {repo_id}.")
                print(f"    Available: {', '.join(ggufs)}")
                return 1
            pick = matches[0]
            print(f"    Filter '{quant_filter}' matched: {pick}")
        else:
            pick = ggufs[0]
            for pref in ("Q4_K_M", "Q5_K_M", "Q8_0", "q4_k_m"):
                for g in ggufs:
                    if pref.lower() in g.lower():
                        pick = g
                        break
                if pick != ggufs[0]:
                    break

        path = hf_hub_download(repo_id=repo_id, filename=pick, local_dir=str(dest), local_dir_use_symlinks=False, token=token)
        print(f"    -> {path}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
