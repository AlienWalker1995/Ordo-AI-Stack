"""docs/canonical/ holds the stack's source-of-truth docs (the PRDs). Agents read them as current
truth, so each one has to say what it is and when it was last checked against the code.

Every doc carries frontmatter:

    authority: canonical
    status: current | superseded
    owner: operator
    last_verified: YYYY-MM-DD        (the day it was last checked against the code)
    verified_against: <commit sha>   (the main commit it was checked against)
    superseded_by: <file>.md         (only when status is superseded; must exist here)

The index links every doc. Changes to this folder are merged by the operator, never auto-merged;
the hold lives in the reviewer's merge gate, not in this repo.
"""
import datetime
import re
from pathlib import Path

import pytest
import yaml

CANONICAL = Path(__file__).resolve().parent.parent / "docs" / "canonical"
DOCS = sorted(CANONICAL.glob("*.md"))
REQUIRED_KEYS = {"authority", "status", "owner", "last_verified", "verified_against"}
STATUSES = {"current", "superseded"}


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
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


def test_the_index_links_every_doc():
    index = (CANONICAL / "index.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(([^)#]+\.md)", index))
    unlinked = [p.name for p in DOCS if p.name != "index.md" and p.name not in linked]
    assert not unlinked, f"index.md does not link {unlinked}"
