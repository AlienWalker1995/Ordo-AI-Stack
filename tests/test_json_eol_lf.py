"""Tracked JSON must stay LF, on every platform.

PR #415 committed five catalogs/fixtures CRLF from a Windows-side edit: the rename diff
inflated (the GPU catalog showed as a 67%-similar rename, the CPU catalog as add+delete)
and the next Linux-side edit would have produced mixed endings. `*.json text eol=lf` in
.gitattributes normalizes checkin AND checkout; these tests keep that rule covering every
tracked JSON file and the committed blobs themselves CR-free, the way
test_image_contexts_lf.py guards the agent image context.
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


def test_every_tracked_json_file_has_an_eol_lf_rule():
    files = git("ls-files", "*.json").split()
    assert files
    attrs = git("check-attr", "eol", "--", *files).splitlines()
    not_lf = [line.split(":")[0] for line in attrs if not line.endswith(": eol: lf")]
    assert not not_lf, f"add an eol=lf rule covering these to .gitattributes: {not_lf}"


def test_no_tracked_json_file_is_committed_crlf():
    # git grep exits 1 when nothing matches — that is the pass case here.
    proc = subprocess.run(
        ["git", "grep", "-l", "\r", "HEAD", "--", "*.json"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    crlf = proc.stdout.split()
    assert not crlf, f"CRLF-committed JSON (convert to LF; *.json eol=lf covers it): {crlf}"
