#!/usr/bin/env python3
"""Sanity-check ComfyUI host paths, checkpoints, workflow refs, ComfyUI HTTP, and output dir.

Prints one JSON object to stdout.

Usage:
  python scripts/comfyui/validate_comfyui_pipeline.py
  python scripts/comfyui/validate_comfyui_pipeline.py --base-path C:/dev/ordo-ai-stack --workflow generate_image --model v1-5-pruned-emaonly.ckpt
  COMFYUI_URL=http://127.0.0.1:8188 python scripts/comfyui/validate_comfyui_pipeline.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _list_checkpoints(models_root: Path) -> list[str]:
    ckpt_dir = models_root / "checkpoints"
    if not ckpt_dir.is_dir():
        return []
    out = []
    for ext in (".safetensors", ".ckpt", ".pth", ".pt", ".sft"):
        out.extend(p.name for p in ckpt_dir.glob(f"*{ext}"))
    return sorted(set(out))


def _extract_ckpt_names_from_workflow(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"<parse_error:{e}>"]
    names: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if "ckpt_name" in o and isinstance(o["ckpt_name"], str):
                names.append(o["ckpt_name"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    return names


def _comfy_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "validate_comfyui_pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(8192).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read(256) or b"").decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate ComfyUI model paths, workflow, and output")
    ap.add_argument("--base-path", type=Path, default=None, help="Repo root (default: infer from script)")
    ap.add_argument("--workflow", type=str, default="generate_image", help="Workflow id stem under data/comfyui-workflows")
    ap.add_argument("--model", type=str, default="", help="Checkpoint filename to verify (e.g. v1-5-pruned-emaonly.ckpt)")
    args = ap.parse_args()

    root = (args.base_path or _repo_root()).resolve()
    models_root = Path(os.environ.get("COMFYUI_MODELS_HOST", str(root / "models" / "comfyui")))
    workflows_dir = root / "data" / "comfyui-workflows"
    output_dir = Path(os.environ.get("COMFYUI_OUTPUT_HOST", str(root / "data" / "comfyui-output")))
    comfy_url = (os.environ.get("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")

    checkpoints = _list_checkpoints(models_root)

    wf_path = workflows_dir / f"{args.workflow}.json"
    wf_ckpts: list[str] = []
    if wf_path.is_file():
        wf_ckpts = _extract_ckpt_names_from_workflow(wf_path)

    model_match: dict | None = None
    literal_ckpt_checks: list[dict] = []
    model = args.model.strip()
    if model:
        ckpt_path = models_root / "checkpoints" / model
        model_match = {"model": model, "exists": ckpt_path.is_file(), "path": str(ckpt_path)}
    elif wf_ckpts and all("PARAM_" not in c for c in wf_ckpts):
        for c in wf_ckpts:
            if "/" in c or "\\" in c:
                continue
            ckpt_path = models_root / "checkpoints" / c
            literal_ckpt_checks.append({"ckpt_name": c, "exists": ckpt_path.is_file(), "path": str(ckpt_path)})

    code, body = _comfy_get(f"{comfy_url}/system_stats")

    out_files: list[str] = []
    if output_dir.is_dir():
        try:
            for p in sorted(output_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:12]:
                if p.is_file():
                    out_files.append(str(p.name))
        except OSError:
            pass

    report = {
        "ok": True,
        "paths": {
            "repo_root": str(root),
            "models_root": str(models_root),
            "workflows_dir": str(workflows_dir),
            "output_dir": str(output_dir),
        },
        "checkpoints": {"count": len(checkpoints), "files": checkpoints[:50]},
        "workflow": {
            "id": args.workflow,
            "path": str(wf_path),
            "exists": wf_path.is_file(),
            "ckpt_refs": wf_ckpts[:30],
        },
        "model_arg_check": model_match,
        "literal_ckpt_checks": literal_ckpt_checks or None,
        "comfyui": {
            "system_stats_url": f"{comfy_url}/system_stats",
            "http_status": code,
            "body_preview": body[:240],
        },
        "output": {
            "dir": str(output_dir),
            "exists": output_dir.is_dir(),
            "recent_files_newest_first": out_files,
        },
        "notes": {
            "compose": "comfyui mounts host models_root → /root/ComfyUI/models; output_dir → /root/ComfyUI/output",
            "ops_download_categories": "dashboard single-file download allowlist may omit unet/vae; pass explicit category or use comfyui-model-puller for full layout",
        },
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
