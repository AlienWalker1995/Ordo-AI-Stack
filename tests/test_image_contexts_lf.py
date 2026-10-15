"""Files COPYed into a Linux image must check out with LF, on every platform.

On a Windows checkout (core.autocrlf=true) a text file without an `eol=lf` attribute is written
with CRLF, and `docker build` copies the working tree as it is. An executable then carries a
`#!/usr/bin/python3\r` shebang and is "not found" inside the container: on 2026-10-15 the agent's
new `github-app-token` helper shipped that way, because .gitattributes listed the agent's
wrappers one by one and the new one was missing. The agent's build context is LF as a whole now;
this test keeps every tracked file in it covered.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTEXTS = ["services/hermes"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("context", CONTEXTS)
def test_every_tracked_file_in_the_image_context_checks_out_lf(context):
    files = git("ls-files", "--", context).split()
    assert files, context
    attrs = git("check-attr", "eol", "--", *files).splitlines()
    not_lf = [line.split(":")[0] for line in attrs if not line.endswith(": eol: lf")]
    assert not not_lf, f"add an eol=lf rule covering these to .gitattributes: {not_lf}"
