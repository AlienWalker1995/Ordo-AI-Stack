"""The contribution-rule checker (scripts/ci/pr_rules.py). Real git repositories, no mocks."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("pr_rules_under_test", ROOT / "scripts" / "ci" / "pr_rules.py")
pr_rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_rules)

EM_DASH = "—"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit(tmp_path, "first")
    return tmp_path


# ------------------------------------------------------------------ this repo


def test_this_repository_has_no_em_dash_in_tracked_markdown():
    assert pr_rules.markdown_findings() == []


# ----------------------------------------------------------------------- text


@pytest.mark.parametrize("text", [
    "fix(rag): the index forgets deleted files",
    "a plain hyphen - is fine, and so is an en dash –",
    "Co-authored-by: Ada Lovelace <ada@example.com>",
    "",
])
def test_ordinary_text_passes(text):
    assert pr_rules.text_findings("commit message", text) == []


@pytest.mark.parametrize("text, expected", [
    (f"one thing {EM_DASH} another", "em dash"),
    ("subject\n\nCo-Authored-By: Claude Example 1.0 <noreply@anthropic.com>", "Co-Authored-By"),
    ("subject\n\nco-authored-by: Copilot <copilot@example.com>", "Co-Authored-By"),
    ("body\n\nGenerated with [Claude Code](https://example.com)", "assistant footer"),
])
def test_rule_breaking_text_is_named_with_its_place(text, expected):
    found = pr_rules.text_findings("pull request body", text)
    assert any(expected in line and line.startswith("pull request body: ") for line in found), found


# ------------------------------------------------------------------- markdown


def test_markdown_findings_name_file_and_line_and_skip_other_files(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_bytes(f"# Guide\n\nfine\nbad {EM_DASH} line\n".encode())
    (repo / "code.py").write_bytes(f'PLACEHOLDER = "{EM_DASH}"\n'.encode())
    (repo / "untracked.md").write_bytes(f"{EM_DASH}\n".encode())
    git(repo, "add", "docs/guide.md", "code.py")
    found = pr_rules.markdown_findings(repo)
    assert len(found) == 1
    assert found[0].startswith("docs/guide.md:4: ")


# -------------------------------------------------------------------- commits


def test_commit_findings_cover_only_commits_after_the_base(repo):
    commit(repo, f"old {EM_DASH} before the base")
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "clean")
    bad = commit(repo, "subject\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    found = pr_rules.commit_findings(base, repo)
    assert found and all(line.startswith(f"commit {bad[:12]} message: ") for line in found)


# --------------------------------------------------------------- pull request


def test_a_pull_request_must_target_the_default_branch():
    stacked = {"PR_RULES_BASE_REF": "feature/part-1", "PR_RULES_DEFAULT_BRANCH": "main"}
    found = pr_rules.pull_request_findings(stacked)
    assert len(found) == 1 and "targets 'feature/part-1', not 'main'" in found[0]
    assert pr_rules.pull_request_findings({"PR_RULES_BASE_REF": "main", "PR_RULES_DEFAULT_BRANCH": "main"}) == []
    assert pr_rules.pull_request_findings({}) == []


def test_a_pull_request_title_and_body_are_checked():
    found = pr_rules.pull_request_findings({"PR_RULES_TITLE": f"a {EM_DASH} b", "PR_RULES_BODY": "clean"})
    assert found == [f"pull request title: {pr_rules.EM_DASH_RULE}"]


# ----------------------------------------------------------------------- main


def test_message_file_mode_ignores_git_comment_lines(tmp_path, capsys):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_bytes(f"clean subject\n# a comment with an em dash {EM_DASH}\n".encode())
    assert pr_rules.main(["--message-file", str(message)]) == 0

    message.write_bytes(f"bad {EM_DASH} subject\n".encode())
    assert pr_rules.main(["--message-file", str(message)]) == 1
    assert "commit message: " in capsys.readouterr().out
