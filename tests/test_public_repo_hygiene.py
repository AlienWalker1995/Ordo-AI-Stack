"""The public-repo guard (scripts/ci/public_hygiene.py), run over this checkout and tested itself.

The guard is a standard-library script so the pre-push hook and CI can run it without installing
anything; this file runs the same checks in the required Pytest job and proves each shape is
caught. The policy is the "What not to commit" section of CONTRIBUTING.md.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
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
    "bind 10.1.2.3:443",
    "peer 172.16.5.9",
    "box.fancy-cat.ts.net",
    "peer 100.101.102.103",
])
def test_identifying_shapes_are_caught(text):
    assert hygiene.findings(text, [])


@pytest.mark.parametrize("text", [
    "https://box.tail1234.ts.net and <tailnet>.ts.net",
    "me@example.com, a@host.example, 12345+bot@users.noreply.github.com",
    "/home/hermes/.hermes and /home/me/dev",
    "discord-123456789012345678",
    "192.168.0.0/16, 172.17.0.0/16 and 10.8.0.0/16 are documented CIDRs",
    "100.64.0.1 and 100.64.0.0/10, resolver 100.100.100.100",
    "react@18.3.1 and image@sha256:abc",
    "host.example.ts.net, tailnet.ts.net and box.tail1234.ts.net",
])
def test_placeholders_pass(text):
    assert hygiene.findings(text, []) == []


@pytest.mark.parametrize("text", [
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "Co-Authored-By: codex <noreply@openclaude.dev>",
])
def test_ai_tool_noreply_trailers_pass(text):
    """A noreply address belongs to a tool, not to a person's mailbox, whatever its domain."""
    assert hygiene.findings(text, []) == []


def test_a_personal_mailbox_at_an_ai_tool_domain_is_still_caught():
    leaks = hygiene.findings("me@example.com was replaced by alice@anthropic.com", [])
    kinds = {description for _, description in leaks}
    assert "an email address outside the placeholder domains" in kinds
    assert len([k for k in kinds if "email" in k]) == 1


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


def test_an_invalid_denylist_line_fails_by_line_number_without_printing_the_name(monkeypatch):
    monkeypatch.setenv("ORDO_HYGIENE_DENYLIST", "fine\nsecretname(\n")
    with pytest.raises(SystemExit) as failure:
        hygiene.operator_denylist()
    message = str(failure.value)
    assert "denylist line 2" in message and "secretname" not in message


def test_a_plain_substring_denylist_catches_a_name_after_an_escape_letter_or_underscore():
    """Why the denylist holds plain substrings: these are the shapes a word boundary let through."""
    denylist = [hygiene.re.compile("myproject", hygiene.re.IGNORECASE)]
    for text in (r'line = "a\tmyproject\tb"', r'pattern = r"\bmyproject\b"', "myproject_backup"):
        assert hygiene.findings(text, denylist), text


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


def _rev(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_a_commit_with_a_personal_address_or_a_leaking_message_is_caught(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "base")
    base = _rev(tmp_path)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "a clean change")
    assert hygiene.commit_leaks(base, [], root=tmp_path) == []
    _commit(tmp_path, "noreply@anthropic.com",
            "another clean change\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    assert hygiene.commit_leaks(base, [], root=tmp_path) == []
    _commit(tmp_path, "someone@gmail.com", "deployed to box.tail1a2b3c.ts.net")
    leaks = hygiene.commit_leaks(base, [], root=tmp_path)
    assert any("author address" in leak for leak in leaks)
    assert any("tailnet id" in leak for leak in leaks)


def test_a_pushed_ref_is_scanned_straight_from_the_object_store(tmp_path):
    """The pre-push hook checks refs that are not checked out: the tree comes from git objects,
    and the commit range ends at the ref's tip instead of HEAD."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "base")
    base = _rev(tmp_path)
    (tmp_path / "bind.txt").write_text("bind 192.168.9.9:443\n", encoding="utf-8")
    subprocess.run(["git", "add", "bind.txt"], cwd=tmp_path, check=True)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "publish the bind address")
    tip = _rev(tmp_path)
    (tmp_path / "bind.txt").write_text("mail someone@gmail.com\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    subprocess.run(["git", "add", "bind.txt", ".env"], cwd=tmp_path, check=True)
    _commit(tmp_path, "1+dev@users.noreply.github.com", "deployed, mail someone@gmail.com")
    newer = _rev(tmp_path)

    # The older tip's tree and range, untouched by the later commit:
    assert any("LAN host address" in leak for leak in hygiene.tracked_file_leaks([], root=tmp_path, at=tip))
    assert not any("email" in leak for leak in hygiene.tracked_file_leaks([], root=tmp_path, at=tip))
    assert hygiene.secret_shaped_files(root=tmp_path, at=tip) == []
    assert hygiene.commit_leaks(base, [], root=tmp_path, end=tip) == []

    # The newer tip sees everything:
    assert any("email" in leak for leak in hygiene.tracked_file_leaks([], root=tmp_path, at=newer))
    assert hygiene.secret_shaped_files(root=tmp_path, at=newer) == [".env"]
    assert any("email address outside the placeholder domains" in leak
               for leak in hygiene.commit_leaks(base, [], root=tmp_path, end=newer))

    # With no baseline at all the whole reachable history is walked instead: the older tip is
    # still clean, the newer tip's history carries the leaking message.
    assert hygiene.commit_leaks(None, [], root=tmp_path, end=tip) == []
    assert hygiene.commit_leaks(None, [], root=tmp_path, end=newer)


def _clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORDO_HYGIENE_")}
    env["GIT_AUTHOR_NAME"] = "Hook Test"
    env["GIT_AUTHOR_EMAIL"] = "1+dev@users.noreply.github.com"
    env["GIT_COMMITTER_NAME"] = "Hook Test"
    env["GIT_COMMITTER_EMAIL"] = "1+dev@users.noreply.github.com"
    return env


def test_the_pre_push_hook_scans_every_ref_git_feeds_it(tmp_path):
    """The hook must check the ref actually being pushed, read from stdin, not the checked-out
    branch: a clean checkout pushing a leaky other-branch or tag used to sail through."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo.mkdir()
    # Put the checker and the hook themselves into the tree so the hook can find both.
    (repo / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "ci" / "public_hygiene.py", repo / "scripts" / "ci" / "public_hygiene.py")
    (repo / ".githooks").mkdir()
    hook_dest = repo / ".githooks" / "pre-push"
    shutil.copy(ROOT / ".githooks" / "pre-push", hook_dest)
    os.chmod(hook_dest, 0o755)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, env=_clean_env(), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    # Establish origin/main so the hook's merge-base against it resolves.
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, env=_clean_env(), check=True)

    # A leaky commit on a separate branch; switch back to clean main before pushing it.
    subprocess.run(["git", "checkout", "-q", "-b", "leaky"], cwd=repo, check=True)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "bind.md").write_text("bind 192.168.9.9:443\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add the bind doc"], cwd=repo, env=_clean_env(), check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)  # working tree is clean again

    # Pushing the leaky branch from a clean checkout must be refused.
    result = subprocess.run(["git", "push", "-q", "origin", "leaky"], cwd=repo,
                            capture_output=True, text=True, env=_clean_env())
    assert result.returncode != 0, f"leaky push was accepted:\n{result.stdout}\n{result.stderr}"
    combined = result.stdout + result.stderr
    assert "LAN host address" in combined, combined

    # A clean ref still goes through: a brand-new, clean commit on main.
    (repo / "notes.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add notes"], cwd=repo, env=_clean_env(), check=True)
    ok = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo,
                        capture_output=True, text=True, env=_clean_env())
    assert ok.returncode == 0, f"clean push was refused:\n{ok.stdout}\n{ok.stderr}"

    # Without a local origin/main tracking ref the baseline falls back to the other
    # remote refs instead of silently dropping the commit check: drop it, push a clean
    # extra branch (so a fallback baseline exists), then push a tag with a leaking message.
    subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "side", "main"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "side"], cwd=repo, env=_clean_env(), check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m",
                    "deployed, mail someone@gmail.com"], cwd=repo, env={
        **_clean_env(), "GIT_AUTHOR_EMAIL": "someone@gmail.com"}, check=True)
    subprocess.run(["git", "tag", "v-msg"], cwd=repo, check=True)
    fallback = subprocess.run(["git", "push", "-q", "origin", "refs/tags/v-msg"], cwd=repo,
                              capture_output=True, text=True, env=_clean_env())
    assert fallback.returncode != 0, f"leaky tag push was accepted without origin/main:\n{fallback.stdout}\n{fallback.stderr}"
    assert "author address" in (fallback.stdout + fallback.stderr)

    # With no remote-tracking refs at all there is no baseline anywhere: every commit reachable
    # from the pushed ref is walked, so a clean-tree tag whose history leaks is still refused.
    subprocess.run(["git", "update-ref", "-d", "refs/remotes/origin/side"], cwd=repo, check=True)
    second = tmp_path / "second.git"
    subprocess.run(["git", "init", "-q", "--bare", str(second)], check=True)
    subprocess.run(["git", "remote", "add", "second", str(second)], cwd=repo, check=True)
    none_baseline = subprocess.run(["git", "push", "-q", "second", "refs/tags/v-msg"], cwd=repo,
                                   capture_output=True, text=True, env=_clean_env())
    assert none_baseline.returncode != 0, \
        f"leaky tag push was accepted with no baseline at all:\n{none_baseline.stdout}\n{none_baseline.stderr}"
    assert "author address" in (none_baseline.stdout + none_baseline.stderr)
