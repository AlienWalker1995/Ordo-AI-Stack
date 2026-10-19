"""Tracked text-class files (JSON/JS/JSX) must stay LF, on every platform.

PR #415 committed five JSON catalogs/fixtures CRLF from a Windows-side edit: the rename
diff inflated (the GPU catalog showed as a 67%-similar rename, the CPU catalog as
add+delete) and the next Linux-side edit would have produced mixed endings. The `*.json
text eol=lf` rule in .gitattributes normalized checkin AND checkout; two frontend test
files (*.js/*.jsx) carried the same pathology into the tree and are covered here too.
These tests keep the eol=lf rule covering every tracked file in those classes and the
committed blobs themselves CR-free, the way test_image_contexts_lf.py guards the agent
image context.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


TEXT_CLASS_PATTERNS = ("*.json", "*.js", "*.jsx")


def test_every_tracked_text_class_file_has_an_eol_lf_rule():
    missing = []
    for pattern in TEXT_CLASS_PATTERNS:
        files = git("ls-files", pattern).split()
        assert files, f"no tracked files matching {pattern}?"
        attrs = git("check-attr", "eol", "--", *files).splitlines()
        missing += [line.split(":")[0] for line in attrs if not line.endswith(": eol: lf")]
    assert not missing, f"add an eol=lf rule covering these to .gitattributes: {missing}"


def test_no_tracked_text_class_file_is_committed_crlf():
    # git grep exits 1 when nothing matches — that is the pass case here.
    offenders = []
    for pattern in TEXT_CLASS_PATTERNS:
        proc = subprocess.run(
            ["git", "grep", "-l", "\r", "HEAD", "--", pattern],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert proc.returncode in (0, 1), proc.stderr
        offenders += proc.stdout.split()
    assert not offenders, f"CRLF-committed text-class files (convert to LF): {offenders}"
