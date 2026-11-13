"""docs/canonical/ holds the stack's source-of-truth docs (the PRDs). Agents read them as current
truth, so each one has to say what it is and when it was last checked against the code.

Every doc carries frontmatter:

    authority: canonical
    status: current | superseded
    owner: operator
    last_verified: YYYY-MM-DD        (the day it was last checked against the code)
    verified_against: <commit sha>   (the main commit it was checked against)
    superseded_by: <file>.md         (only when status is superseded; must exist here)

The index links every doc, and every relative link in a doc points at a file that exists. Changes
to this folder are the repository owner's alone (.github/CODEOWNERS, ADR-0003).
"""
import datetime
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "docs" / "canonical"
DOCS = sorted(CANONICAL.rglob("*.md"))
# A relative markdown link: [text](target), target without a scheme. The anchor is not checked.
RELATIVE_LINK = re.compile(r"\]\((?![a-z][a-z0-9+.-]*:|#)([^)#\s]+)")
REQUIRED_KEYS = {"authority", "status", "owner", "last_verified", "verified_against"}
STATUSES = {"current", "superseded"}


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match, f"{path.name}: no YAML frontmatter at the top"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), f"{path.name}: frontmatter is not a mapping"
    return data


def test_the_canonical_folder_has_docs():
    assert DOCS, "docs/canonical/ is empty"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_each_doc_declares_its_authority_and_verification(path: Path):
    meta = frontmatter(path)
    missing = REQUIRED_KEYS - meta.keys()
    assert not missing, f"{path.name}: frontmatter is missing {sorted(missing)}"
    assert meta["authority"] == "canonical", f"{path.name}: authority must be 'canonical'"
    assert meta["status"] in STATUSES, f"{path.name}: status must be one of {sorted(STATUSES)}"
    assert meta["owner"] == "operator", f"{path.name}: owner must be 'operator'"

    verified = meta["last_verified"]
    assert isinstance(verified, datetime.date), f"{path.name}: last_verified must be a YYYY-MM-DD date"
    assert verified <= datetime.date.today(), f"{path.name}: last_verified is in the future"
    assert re.fullmatch(r"[0-9a-f]{7,40}", str(meta["verified_against"])), \
        f"{path.name}: verified_against must be a commit sha"

    if meta["status"] == "superseded":
        successor = meta.get("superseded_by")
        assert successor, f"{path.name}: a superseded doc names its successor in superseded_by"
        assert (CANONICAL / successor).is_file(), f"{path.name}: superseded_by {successor} does not exist"
    else:
        assert "superseded_by" not in meta, f"{path.name}: only a superseded doc has superseded_by"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _has_full_history() -> bool:
    """False in a shallow clone or outside git, where an older commit cannot be looked up."""
    return _git("rev-parse", "--is-shallow-repository").stdout.strip() == "false"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_verified_against_is_a_commit_in_this_history(path: Path):
    """A sha that only looks like one proves nothing: it has to be a commit HEAD descends from."""
    if not _has_full_history():
        pytest.skip("shallow or non-git checkout: the commit cannot be looked up")
    sha = str(frontmatter(path)["verified_against"])
    reachable = _git("merge-base", "--is-ancestor", f"{sha}^{{commit}}", "HEAD")
    assert reachable.returncode == 0, f"{path.name}: verified_against {sha} is not a commit in this history"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_relative_link_resolves(path: Path):
    text = path.read_text(encoding="utf-8")
    broken = [target for target in RELATIVE_LINK.findall(text) if not (path.parent / target).exists()]
    assert not broken, f"{path.name}: links to files that do not exist: {broken}"


def test_the_index_links_every_doc():
    index = (CANONICAL / "index.md").read_text(encoding="utf-8")
    linked = {(CANONICAL / target).resolve() for target in RELATIVE_LINK.findall(index)}
    unlinked = [str(p.relative_to(CANONICAL)) for p in DOCS if p.name != "index.md" and p.resolve() not in linked]
    assert not unlinked, f"index.md does not link {unlinked}"
