"""A build context's .dockerignore must never exclude the code the image needs.

The dashboard's defense-in-depth pattern `*secret*` silently dropped `secret_env.py` (the
_FILE secret reader added in #269) from the image, and the dashboard crash-looped on
`ModuleNotFoundError: dashboard.secret_env`. Ignore patterns must target secret MATERIAL
(env files, keys), never source files.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".sh")


def _patterns(ignore_file: Path) -> list[str]:
    lines = ignore_file.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("#", "!"))]


def _excluded(rel: str, patterns: list[str]) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        bare = pattern.lstrip("/").rstrip("/")
        if fnmatch.fnmatch(rel, bare) or fnmatch.fnmatch(name, bare) or rel.startswith(bare + "/"):
            return pattern
    return None


def test_no_tracked_source_file_is_excluded_from_its_build_context():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    offenders = []
    for ignore in (ROOT / p for p in tracked if p.endswith(".dockerignore") and p != ".dockerignore"):
        context = ignore.parent
        patterns = _patterns(ignore)
        prefix = context.relative_to(ROOT).as_posix() + "/"
        for path in tracked:
            if not path.startswith(prefix) or not path.endswith(SOURCE_SUFFIXES):
                continue
            rel = path[len(prefix):]
            if "node_modules/" in rel or "/dist/" in rel:
                continue
            hit = _excluded(rel, patterns)
            if hit:
                offenders.append(f"{path} excluded by '{hit}' in {ignore.relative_to(ROOT).as_posix()}")
    assert offenders == []
