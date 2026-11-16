#!/usr/bin/env python3
"""Fail when a change breaks one of the repository's contribution rules (CONTRIBUTING.md).

These rules used to be instructions to contributors and agents. An instruction is advice; this
is the check. Standard library only.

  1. A pull request targets the default branch. A stacked pull request merges into its base
     branch, not into the default branch, and its content never ships.
  2. No AI-assistant attribution in a commit message or in a pull request's title and body.
  3. No em dash in a commit message, in a pull request's title and body, or in a tracked
     Markdown file. Use a comma, a colon, parentheses or a plain hyphen.

What is checked:
  - tracked Markdown files, always;
  - with --base SHA, the message of every commit after SHA up to HEAD;
  - with --message-file PATH, that one message (the commit-msg hook);
  - the pull request's text and target, from the environment: PR_RULES_TITLE, PR_RULES_BODY,
    PR_RULES_BASE_REF and PR_RULES_DEFAULT_BRANCH (a workflow sets them; unset means not a
    pull request).

Exit code 1 and one line per finding, naming the place and the rule.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EM_DASH = "—"
EM_DASH_RULE = "has an em dash (use a comma, a colon, parentheses or a plain hyphen)"
ATTRIBUTION = (
    (re.compile(r"(?im)^co-authored-by:.*\b(claude|copilot|cursor|codex|gemini|chatgpt)\b"),
     "has a Co-Authored-By line naming an AI assistant"),
    (re.compile(r"(?i)noreply@anthropic\.com"), "has an AI vendor's no-reply address"),
    (re.compile(r"(?i)generated with \[?claude code"), "has a 'Generated with' assistant footer"),
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def text_findings(label: str, text: str) -> list[str]:
    """The rules a commit message or a pull request's text breaks."""
    found = [f"{label}: {description}" for pattern, description in ATTRIBUTION if pattern.search(text)]
    if EM_DASH in text:
        found.append(f"{label}: {EM_DASH_RULE}")
    return found


def markdown_findings(root: Path = ROOT) -> list[str]:
    found = []
    for name in _git(root, "ls-files", "-z", "--", "*.md").split("\0"):
        if not name:
            continue
        try:
            lines = (root / name).read_bytes().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue    # tracked but absent from this checkout (a sparse or mid-merge tree)
        for number, line in enumerate(lines, start=1):
            if EM_DASH in line:
                found.append(f"{name}:{number}: {EM_DASH_RULE}")
    return found


def commit_findings(base: str, root: Path = ROOT, end: str = "HEAD") -> list[str]:
    found = []
    for sha in _git(root, "rev-list", f"{base}..{end}").split():
        message = _git(root, "show", "-s", "--format=%B", sha)
        found.extend(text_findings(f"commit {sha[:12]} message", message))
    return found


def pull_request_findings(environment: dict[str, str]) -> list[str]:
    found = []
    base_ref = environment.get("PR_RULES_BASE_REF", "").strip()
    default_branch = environment.get("PR_RULES_DEFAULT_BRANCH", "").strip()
    if base_ref and default_branch and base_ref != default_branch:
        found.append(f"pull request: targets '{base_ref}', not '{default_branch}' (a stacked pull request "
                     f"merges into its base and never ships; retarget it to '{default_branch}')")
    for label, variable in (("pull request title", "PR_RULES_TITLE"), ("pull request body", "PR_RULES_BODY")):
        found.extend(text_findings(label, environment.get(variable, "")))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a change breaks a contribution rule.")
    parser.add_argument("--base", help="also check the message of every commit after this one")
    parser.add_argument("--message-file", type=Path, help="check this one commit message and nothing else")
    args = parser.parse_args(argv)

    if args.message_file is not None:
        # Git comment lines (the commit template's "# ..." help) are not part of the message.
        lines = args.message_file.read_bytes().decode("utf-8", errors="replace").splitlines()
        message = "\n".join(line for line in lines if not line.startswith("#"))
        found = text_findings("commit message", message)
    else:
        found = markdown_findings()
        if args.base:
            found.extend(commit_findings(args.base))
        found.extend(pull_request_findings(dict(os.environ)))

    for line in found:
        print(line)
    if found:
        print(f"pr rules: {len(found)} finding(s); the rules are in CONTRIBUTING.md", file=sys.stderr)
        return 1
    print("pr rules: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
