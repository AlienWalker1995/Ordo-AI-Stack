"""The public-repo guard (scripts/ci/public_hygiene.py), run over this checkout and tested itself.

The guard is a standard-library script so the pre-push hook and CI can run it without installing
anything; this file runs the same checks in the required Pytest job and proves each shape is
caught. The policy is docs/canonical/adr-0002-nothing-published-identifies-the-operator.md.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("public_hygiene", ROOT / "scripts" / "ci" / "public_hygiene.py")
hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hygiene)


def test_no_tracked_file_identifies_the_operator():
    leaks = hygiene.tracked_file_leaks(hygiene.operator_denylist())
    assert not leaks, "public repo leaks:\n" + "\n".join(leaks)


def test_no_secret_shaped_file_is_tracked():
    shaped = hygiene.secret_shaped_files()
    assert not shaped, f"secret-shaped files are tracked: {shaped}"


@pytest.mark.parametrize("text", [
    "https://box.tail1a2b3c.ts.net",
    "someone@gmail.com",
    "C:\\Users\\alice\\dev",
    "/home/alice/.ssh",
    "discord-987654321098765432",
    "bind 192.168.1.20:443",
    "peer 100.101.102.103",
])
def test_identifying_shapes_are_caught(text):
    assert hygiene.findings(text, [])


@pytest.mark.parametrize("text", [
    "https://box.tail1234.ts.net and <tailnet>.ts.net",
    "me@example.com, a@host.example, 12345+bot@users.noreply.github.com",
    "/home/hermes/.hermes and /home/me/dev",
    "discord-123456789012345678",
    "192.168.0.0/16, 172.18.0.5, 10.9.0.7",
    "100.64.0.1 and 100.64.0.0/10, resolver 100.100.100.100",
    "react@18.3.1 and image@sha256:abc",
])
def test_placeholders_pass(text):
    assert hygiene.findings(text, []) == []


def test_a_finding_never_repeats_the_matched_text():
    """CI logs are public: a finding says where and what kind, not what."""
    for _, description in hygiene.findings("mail someone@gmail.com from /home/alice", []):
        assert "gmail" not in description and "alice" not in description


def test_the_denylist_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDO_HYGIENE_DENYLIST", "# a comment\n\\bmyhost\\b\n")
    assert hygiene.findings("ssh MyHost", hygiene.operator_denylist()) == [(1, "a name on the operator's denylist")]
    listed = tmp_path / "denylist.txt"
    listed.write_text("my-other-project\n", encoding="utf-8")
    monkeypatch.setenv("ORDO_HYGIENE_DENYLIST", "")
    monkeypatch.setenv("ORDO_HYGIENE_DENYLIST_FILE", str(listed))
    assert hygiene.findings("cd my-other-project", hygiene.operator_denylist())


@pytest.mark.parametrize("name,shaped", [
    ("secrets/.env.sops", True), ("certs/edge.key", True), (".env", True), ("app/.env.prod", True),
    (".env.example", False), ("docs/runbooks/secrets.md", False), ("secrets/README.md", False),
])
def test_secret_shaped_file_names(name, shaped):
    assert bool(hygiene.SECRET_SHAPED_FILE.search(name)) is shaped


def _commit(repo: Path, email: str, message: str) -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": email, "PATH": os.environ["PATH"]}
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=repo, env=env, check=True)


def test_a_commit_with_a_personal_address_or_a_leaking_message_is_caught(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True,
                          check=True).stdout.strip()
    _commit(tmp_path, "1+dev@users.noreply.github.com", "a clean change")
    assert hygiene.commit_leaks(base, [], root=tmp_path) == []
    _commit(tmp_path, "someone@gmail.com", "deployed to box.tail1a2b3c.ts.net")
    leaks = hygiene.commit_leaks(base, [], root=tmp_path)
    assert any("author address" in leak for leak in leaks)
    assert any("tailnet id" in leak for leak in leaks)
