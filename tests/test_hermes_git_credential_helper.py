"""The git credential helper Hermes uses for github.com (services/hermes/git-credential-github-pat).

It must answer `get` with the PAT read from its file secret, answer nothing when the secret is
missing or empty (so git fails with its own authentication error), and ignore `store`/`erase`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "services" / "hermes" / "git-credential-github-pat"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def run(action: str, token_file: Path) -> str:
    result = subprocess.run(["sh", str(HELPER), action], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, check=True,
                            env={"GITHUB_BACKUP_PAT_FILE": str(token_file), "PATH": "/usr/bin:/bin"})
    return result.stdout


def test_get_answers_with_the_token_from_the_file(tmp_path):
    token = tmp_path / "github_backup_pat"
    token.write_text("ghp_example_token_value")
    assert run("get", token) == "username=x-access-token\npassword=ghp_example_token_value\n"


def test_get_answers_nothing_without_a_token(tmp_path):
    empty = tmp_path / "github_backup_pat"
    empty.write_text("")
    assert run("get", empty) == ""
    assert run("get", tmp_path / "absent") == ""


@pytest.mark.parametrize("action", ["store", "erase"])
def test_store_and_erase_do_nothing(tmp_path, action):
    token = tmp_path / "github_backup_pat"
    token.write_text("ghp_example_token_value")
    assert run(action, token) == ""


def test_the_image_wires_the_helper_for_github_only():
    dockerfile = (HELPER.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "git config --system credential.https://github.com.helper /usr/local/bin/git-credential-github-pat" in dockerfile
