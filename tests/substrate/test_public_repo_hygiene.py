"""The Ordo repo is public: no tracked file may name the operator's tailnet, host, or private repo.

A concrete tailnet id (`tail1a2b3c.ts.net`), the host's name, or the operator's private secrets
repo name in a README or a test fixture tells anyone reading the repo where the stack lives, or
what to go looking for. Docs and fixtures use placeholders instead: `<tailnet>.ts.net` in prose,
`example.ts.net` in tests, `../ordo-secrets/secrets.env.sops` for the SOPS file's documented default.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = {
    "a concrete tailnet id": re.compile(r"\btail[0-9a-f]{6,}\b"),
    "the operator's host name": re.compile(r"(?i)\bultracam\b"),
    "the operator's private secrets repo name": re.compile(r"\bordo-personal\b"),
}

# Files allowed to spell a forbidden shape: the privacy guards themselves, which have to write
# the pattern down to search for it, and CHANGELOG.md, an append-only history of already-published
# entries (rewriting past entries would falsify the record; the name only ever named an example path).
ALLOWED = {"tests/evals/test_datasets.py", "tests/substrate/test_public_repo_hygiene.py", "CHANGELOG.md"}


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
