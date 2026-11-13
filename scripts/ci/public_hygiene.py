#!/usr/bin/env python3
"""This repo is public: nothing published from it may identify the operator or their deployment.

"Published" is more than tracked files. A pull request's title and body, a commit message and a
commit's author address are public the moment they are pushed, so the same checks run over all of
them. The policy is docs/canonical/adr-0002-nothing-published-identifies-the-operator.md.

Two layers:

- Generic shapes, always checked: a concrete tailnet id, a real-looking email address, a LAN or
  tailnet host address, a user's home path, a chat-platform id, and any tracked file shaped like
  a secret store.
- The operator's own names (host, domain, other projects, private repo), which cannot be written
  here without publishing them. They come from outside the repo, one regular expression per line
  (plain substrings: a word boundary misses a name that follows an escape letter or an underscore):
  the ORDO_HYGIENE_DENYLIST variable (CI sets it from a repository secret), or the file named by
  ORDO_HYGIENE_DENYLIST_FILE.

A finding names the file and line and what kind of thing was found, never the matched text: CI logs
are public too.

Standard library only, so the pre-push hook (.githooks/pre-push) can run it anywhere git runs.

    python scripts/ci/public_hygiene.py                 # the tracked files
    python scripts/ci/public_hygiene.py --base <sha>    # and every commit after <sha>

The pull request's text is checked when ORDO_HYGIENE_PR_TITLE / ORDO_HYGIENE_PR_BODY are set.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Domains an example address may use: the reserved documentation names, the short invented ones
# the fixtures already use, and GitHub's own (noreply author addresses, web-flow committers).
PLACEHOLDER_EMAIL_DOMAINS = re.compile(
    r"(?i)(^|\.)(example\.(com|org|net)|example|invalid|test|localhost|local|"
    r"x\.io|y\.io|y\.co\.uk|domain\.tld|github\.com)$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}|localhost)")

# Home directories that belong to a container image or a placeholder, not to a person.
PLACEHOLDER_HOME_NAMES = {"hermes", "node", "evals", "me", "you", "user", "runner", "app", "ubuntu"}
HOME_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/c/users/|/users/|/home/)([A-Za-z0-9._-]+)")

# The two obviously invented ids the fixtures use.
PLACEHOLDER_IDS = {"123456789012345678", "112233445566778899"}
CHAT_ID = re.compile(r"(?<!\d)\d{17,20}(?!\d)")

TAILNET_ID = re.compile(r"\btail[0-9a-f]{6,}\b")
# A host address on a home LAN. Docker bridge ranges and the documentation CIDRs are not identifying.
LAN_HOST = re.compile(r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d/])")
# A tailnet host address. 100.64.0.x is the placeholder range the docs and fixtures use, and
# 100.100.100.100 is the tailnet provider's own well-known resolver, the same on every tailnet.
TAILNET_HOST = re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d/])")
TAILNET_PLACEHOLDER = re.compile(r"^(100\.64\.0\.\d{1,3}|100\.100\.100\.100)$")

SECRET_SHAPED_FILE = re.compile(
    r"(?i)(\.(sops|age|pem|key|p12|pfx)$|(^|/)\.env$|(^|/)\.env\.(?!example$)[a-z0-9_-]+$)")

# Files allowed to spell a forbidden shape: the guard's own tests (they write the shapes down) and
# third-party licence texts, which carry their authors' addresses.
ALLOWED_FILES = {"tests/test_public_repo_hygiene.py"}
ALLOWED_SUFFIXES = ("-OFL.txt", "LICENSE", "LICENSE.txt", "NOTICE")


def operator_denylist() -> list[re.Pattern[str]]:
    """The operator's own names, from outside the repo. Empty when neither variable is set."""
    text = os.environ.get("ORDO_HYGIENE_DENYLIST", "")
    file_path = os.environ.get("ORDO_HYGIENE_DENYLIST_FILE", "").strip()
    if not text.strip() and file_path:
        text = Path(file_path).read_text(encoding="utf-8")
    patterns = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def findings(text: str, denylist: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    """Every (line number, description) in `text` that would identify the operator."""
    found: list[tuple[int, str]] = []

    def add(match: re.Match[str], description: str) -> None:
        found.append((text.count("\n", 0, match.start()) + 1, description))

    for match in TAILNET_ID.finditer(text):
        add(match, "a concrete tailnet id")
    for match in EMAIL.finditer(text):
        if not PLACEHOLDER_EMAIL_DOMAINS.search(match.group(1)):
            add(match, "an email address outside the placeholder domains")
    for match in HOME_PATH.finditer(text):
        if match.group(1).lower() not in PLACEHOLDER_HOME_NAMES:
            add(match, "a user's home path")
    for match in CHAT_ID.finditer(text):
        if match.group(0) not in PLACEHOLDER_IDS:
            add(match, "a chat-platform id (17 to 20 digits)")
    for match in LAN_HOST.finditer(text):
        add(match, "a LAN host address")
    for match in TAILNET_HOST.finditer(text):
        if not TAILNET_PLACEHOLDER.match(match.group(0)):
            add(match, "a tailnet host address")
    for pattern in denylist:
        for match in pattern.finditer(text):
            add(match, "a name on the operator's denylist")
    return sorted(found)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def tracked_files(root: Path = ROOT) -> list[str]:
    return [name for name in _git(root, "ls-files", "-z").split("\0") if name]


def tracked_file_leaks(denylist: list[re.Pattern[str]], root: Path = ROOT) -> list[str]:
    """Leaks in the content and the paths of the tracked files."""
    leaks = []
    for name in tracked_files(root):
        if name in ALLOWED_FILES or name.endswith(ALLOWED_SUFFIXES):
            continue
        # A file's own path is published too.
        leaks += [f"{name}: its path holds {description}" for _, description in findings(name, denylist)]
        path = root / name
        if not path.is_file():  # the index can briefly list a file the working tree no longer has
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary asset
        leaks += [f"{name}:{line}: {description}" for line, description in findings(text, denylist)]
    return leaks


def secret_shaped_files(root: Path = ROOT) -> list[str]:
    """An encrypted secret store is still a secret store: ciphertext, once published, is permanent,
    and it is one leaked key away from plaintext. The operator's store lives in a private repo."""
    return [name for name in tracked_files(root) if SECRET_SHAPED_FILE.search(name)]


def text_leaks(label: str, text: str, denylist: list[re.Pattern[str]]) -> list[str]:
    return [f"{label} line {line}: {description}" for line, description in findings(text, denylist)]


def commit_leaks(base: str, denylist: list[re.Pattern[str]], root: Path = ROOT) -> list[str]:
    """Every commit after `base`: its message, and its author and committer addresses, which must
    be GitHub noreply addresses (or a bot's placeholder), never a personal mailbox."""
    log = _git(root, "log", "--format=%H%x1f%ae%x1f%ce%x1f%B%x1e", f"{base}..HEAD")
    leaks = []
    for record in filter(str.strip, log.split("\x1e")):
        sha, author, committer, message = record.strip().split("\x1f", 3)
        for role, address in (("author", author), ("committer", committer)):
            if not PLACEHOLDER_EMAIL_DOMAINS.search(address.rpartition("@")[2]):
                leaks.append(f"{sha[:12]}: the {role} address is not a noreply or placeholder address")
        leaks += text_leaks(f"{sha[:12]}: message", message, denylist)
    return leaks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when anything about to be published identifies the operator.")
    parser.add_argument("--base", help="also check every commit after this one (messages and addresses)")
    args = parser.parse_args(argv)

    denylist = operator_denylist()
    leaks = tracked_file_leaks(denylist)
    leaks += [f"{name}: a secret-shaped file is tracked" for name in secret_shaped_files()]
    if args.base:
        leaks += commit_leaks(args.base, denylist)
    title, body = os.environ.get("ORDO_HYGIENE_PR_TITLE"), os.environ.get("ORDO_HYGIENE_PR_BODY")
    if title is not None:
        leaks += text_leaks("pull request title", title, denylist)
    if body is not None:
        leaks += text_leaks("pull request body", body, denylist)

    layers = "generic shapes and the operator's denylist" if denylist else "generic shapes only (no denylist set)"
    if leaks:
        print(f"public repo hygiene: {len(leaks)} leak(s) [{layers}]", file=sys.stderr)
        print("\n".join(leaks), file=sys.stderr)
        print("Policy and fixes: docs/canonical/adr-0002-nothing-published-identifies-the-operator.md", file=sys.stderr)
        return 1
    print(f"public repo hygiene: clean [{layers}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
