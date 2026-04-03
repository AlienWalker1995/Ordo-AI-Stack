#!/usr/bin/env python3
"""Dry-run-first cleanup for known runtime caches under data/."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _known_targets(root: Path) -> list[Path]:
    data = root / "data"
    targets = [
        data / "open-webui" / "cache" / "embedding",
        data / "open-webui" / "cache" / "sentence_transformers",
        data / "comfyui-storage" / ".cache",
        data / "comfyui-storage" / ".local" / "pip" / "cache",
    ]
    custom_nodes = data / "comfyui-storage" / "ComfyUI" / "custom_nodes"
    if custom_nodes.is_dir():
        for git_dir in custom_nodes.rglob(".git"):
            targets.append(git_dir)
    return targets


def _size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _format_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def _safe_remove(path: Path, repo_root: Path) -> None:
    resolved = path.resolve()
    resolved.relative_to((repo_root / "data").resolve())
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually remove the detected cache targets.")
    args = parser.parse_args()

    root = _repo_root()
    targets = [path for path in _known_targets(root) if path.exists()]
    if not targets:
        print("No known cache targets found.")
        return 0

    total = 0
    for path in targets:
        size = _size_bytes(path)
        total += size
        print(f"{'REMOVE' if args.apply else 'DRY-RUN'} {path.relative_to(root)} {_format_mb(size)}")
        if args.apply:
            _safe_remove(path, root)

    print(f"{'Removed' if args.apply else 'Would remove'} total: {_format_mb(total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
