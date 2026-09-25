"""Every `ordo/<path>.py` a tracked file mentions exists in the repo.

Comments, docs and READMEs point readers at the module that owns a behaviour
(`ordo/render/compose.py::_gpu_gate`, `ordo/control/api.py`'s `ControlPlane`). When a module moves
(PR #271 split the flat `ordo/*.py` into `render/`, `control/` and `host/`), a pointer to the old
path silently sends the reader nowhere. This test fails on any such dangling path.

CHANGELOG.md is exempt: it records history, so it names paths as they were at the time.
A path with a glob (`ordo/host/cli_*.py`) must match at least one file.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXEMPT_FILES = {"CHANGELOG.md", "test_module_path_references.py"}  # compared by file name; this file names moved paths as test input

# A repo-relative `ordo/...py` path. The lookbehind skips paths that are part of a longer one
# (`/app/ordo/x.py` inside a container, `services/ordo-foo/x.py`), which are not repo paths.
MODULE_PATH = re.compile(r"(?<![\w./-])ordo/[\w/*]+\.py\b")


def tracked_text_files() -> list[Path]:
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    paths = [ROOT / name for name in listing.decode("utf-8").split("\0") if name]
    return [path for path in paths if path.name not in EXEMPT_FILES and path.is_file()]


def path_exists(reference: str) -> bool:
    if "*" in reference:
        return any(ROOT.glob(reference))
    return (ROOT / reference).is_file()


def dangling_in(text: str) -> list[tuple[int, str]]:
    """(line number, path) for each `ordo/...py` path in `text` that does not exist in the repo."""
    dangling = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for reference in MODULE_PATH.findall(line):
            if not path_exists(reference):
                dangling.append((line_number, reference))
    return dangling


def test_every_referenced_ordo_module_path_exists():
    findings = []
    for path in tracked_text_files():
        data = path.read_bytes()
        if b"\x00" in data:
            continue  # binary
        for line_number, reference in dangling_in(data.decode("utf-8", errors="replace")):
            findings.append(f"{path.relative_to(ROOT).as_posix()}:{line_number}: {reference}")
    assert findings == []


def test_the_scan_flags_a_moved_module_and_accepts_real_paths():
    text = (
        "see ordo/compose.py::_gpu_gate and ordo/render/compose.py, ordo/cli.py, ordo/host/cli_*.py\n"
        "a container path such as /app/ordo/compose.py is not a repo path\n"
    )
    assert dangling_in(text) == [(1, "ordo/compose.py")]
