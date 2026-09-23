"""The Ordo repo is public: no tracked file may name the operator's tailnet or host.

A concrete tailnet id (`tail1a2b3c.ts.net`) or the host's name in a README or a test fixture
tells anyone reading the repo where the stack lives. Docs and fixtures use placeholders instead:
`<tailnet>.ts.net` in prose, `example.ts.net` in tests.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = {
    "a concrete tailnet id": re.compile(r"\btail[0-9a-f]{6,}\b"),
    "the operator's host name": re.compile(r"(?i)\bultracam\b"),
}

# Files allowed to spell a forbidden shape: the privacy guards themselves, which have to write
# the pattern down to search for it.
ALLOWED = {"tests/evals/test_datasets.py", "tests/substrate/test_public_repo_hygiene.py"}


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True,
                            check=True).stdout.split("\0")
    files = []
    for name in listed:
        path = ROOT / name
        # exists() guard: the index can briefly list a file the working tree no longer has.
        if name and name not in ALLOWED and path.is_file():
            files.append(path)
    return files


def test_no_tracked_file_names_the_operators_tailnet_or_host():
    leaks = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary asset
        for description, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                leaks.append(f"{path.relative_to(ROOT).as_posix()}:{line}: {description}")
    assert not leaks, "public repo leaks:\n" + "\n".join(leaks)
