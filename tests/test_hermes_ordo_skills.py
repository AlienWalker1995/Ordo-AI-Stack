"""services/hermes/scripts/ordo_skills.py: repo-owned skills that Hermes cannot rewrite or lose.

The ComfyUI skill is the one this exists for. It used to live only in the agent's home volume,
where it had no version control and Hermes rewrites its own SKILL.md, and its scripts submitted
straight to the engine, skipping the GPU admission gate. It now ships from the image with a small
overlay, and these tests pin the pieces that keep that true:

* the overlay markdown lands before the upstream quick start, not after it;
* registering the shipped dir never drops an operator's own external_dirs entries;
* a local skill with the same frontmatter name is found and moved aside, never deleted, because
  Hermes lists local skills first and silently skips a same-named external one;
* the overlay diff makes the scripts follow COMFYUI_URL and keeps the stock fallback;
* the overlay files carry nothing deployment-specific, because this repo is public.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "services" / "hermes" / "skill-overlays" / "comfyui"

_spec = importlib.util.spec_from_file_location(
    "ordo_skills_under_test", REPO / "services" / "hermes" / "scripts" / "ordo_skills.py"
)
ordo_skills = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ordo_skills)


UPSTREAM_LIKE_SKILL = """---
name: comfyui
description: Generate images.
---

# ComfyUI

Intro paragraph.

```bash
## not a heading, this is inside a fence
```

## What's in this skill

Stuff.

## Quick Start

curl http://127.0.0.1:8188/system_stats
"""


# --- build: where the overlay goes ---

def test_overlay_is_inserted_before_the_first_real_h2():
    out = ordo_skills.insert_before_first_h2(UPSTREAM_LIKE_SKILL, "## Ordo notes\n\nUse the gate.\n")
    assert out.index("## Ordo notes") < out.index("## What's in this skill")
    assert out.index("## Ordo notes") < out.index("## Quick Start")
    assert out.index("# ComfyUI") < out.index("## Ordo notes")  # title and intro stay first


def test_a_heading_inside_a_code_fence_is_not_the_anchor():
    out = ordo_skills.insert_before_first_h2(UPSTREAM_LIKE_SKILL, "## Ordo notes\n")
    assert out.index("## not a heading") < out.index("## Ordo notes")


def test_frontmatter_is_left_intact():
    out = ordo_skills.insert_before_first_h2(UPSTREAM_LIKE_SKILL, "## Ordo notes\n")
    assert out.startswith("---\nname: comfyui\ndescription: Generate images.\n---\n")


def test_nothing_else_is_changed():
    block = "## Ordo notes\n\nUse the gate.\n\n"
    out = ordo_skills.insert_before_first_h2(UPSTREAM_LIKE_SKILL, block)
    assert out.replace(block, "", 1) == UPSTREAM_LIKE_SKILL


def test_a_skill_md_with_no_h2_fails_the_build_instead_of_guessing():
    with pytest.raises(ValueError):
        ordo_skills.insert_before_first_h2("---\nname: x\n---\n# Title\n\nno sections\n", "## notes\n")


def test_build_copies_the_skill_and_splices_the_overlay(tmp_path):
    bundled = tmp_path / "bundled" / "comfyui"
    (bundled / "scripts").mkdir(parents=True)
    (bundled / "SKILL.md").write_text(UPSTREAM_LIKE_SKILL, encoding="utf-8")
    (bundled / "scripts" / "_common.py").write_text("x = 1\n", encoding="utf-8")
    (bundled / "scripts" / "__pycache__").mkdir()
    (bundled / "scripts" / "__pycache__" / "junk.pyc").write_bytes(b"\0")
    overlay = tmp_path / "overlay.md"
    overlay.write_text("## Ordo notes\n", encoding="utf-8")
    dest = tmp_path / "shipped" / "creative" / "comfyui"

    ordo_skills.build(bundled, overlay, dest)

    assert (dest / "scripts" / "_common.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (dest / "scripts" / "__pycache__").exists()
    assert "## Ordo notes" in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_build_refuses_to_merge_into_an_existing_copy(tmp_path):
    bundled = tmp_path / "b"
    bundled.mkdir()
    (bundled / "SKILL.md").write_text(UPSTREAM_LIKE_SKILL, encoding="utf-8")
    dest = tmp_path / "d"
    dest.mkdir()
    overlay = tmp_path / "o.md"
    overlay.write_text("## n\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ordo_skills.build(bundled, overlay, dest)


# --- boot: registering the shipped dir ---

@pytest.mark.parametrize("current, expected", [
    ([], ["/opt/ordo-skills"]),
    (None, ["/opt/ordo-skills"]),
    ("", ["/opt/ordo-skills"]),
    (["/team/skills"], ["/team/skills", "/opt/ordo-skills"]),
    ("/team/skills", ["/team/skills", "/opt/ordo-skills"]),
])
def test_registration_appends_and_keeps_the_operators_entries(current, expected):
    assert ordo_skills.merged_external_dirs(current, "/opt/ordo-skills") == expected


def test_registration_is_a_no_op_when_already_present():
    assert ordo_skills.merged_external_dirs(["/a", "/opt/ordo-skills"], "/opt/ordo-skills") is None


def test_a_bare_string_already_naming_the_dir_is_normalised_to_a_list():
    assert ordo_skills.merged_external_dirs("/opt/ordo-skills", "/opt/ordo-skills") == ["/opt/ordo-skills"]


# --- boot: shadows ---

def _skill(root: Path, rel: str, name: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n# {name}\n", encoding="utf-8")
    return d


def test_a_same_named_local_skill_is_found_even_at_a_different_path(tmp_path):
    shipped, local = tmp_path / "shipped", tmp_path / "local"
    _skill(shipped, "creative/comfyui", "comfyui")
    moved_category = _skill(local, "media/renamed-dir", "comfyui")
    _skill(local, "creative/comfyui-notes", "comfyui-notes")
    assert ordo_skills.find_shadows(local, shipped) == [moved_category]


def test_hermes_bookkeeping_dirs_are_not_treated_as_skills(tmp_path):
    shipped, local = tmp_path / "shipped", tmp_path / "local"
    _skill(shipped, "creative/comfyui", "comfyui")
    _skill(local, ".archive/creative/comfyui", "comfyui")
    _skill(local, ".curator_backups/x/comfyui", "comfyui")
    assert ordo_skills.find_shadows(local, shipped) == []


def test_a_shadow_is_moved_aside_with_its_contents_not_deleted(tmp_path):
    local, archive = tmp_path / "skills", tmp_path / "ordo-shadowed-skills"
    shadow = _skill(local, "creative/comfyui", "comfyui")
    (shadow / "references").mkdir()
    (shadow / "references" / "notes.md").write_text("keep me", encoding="utf-8")

    moved = ordo_skills.move_aside(shadow, local, archive, "20260923T000000Z")

    assert not shadow.exists()
    assert moved == archive / "20260923T000000Z" / "creative" / "comfyui"
    assert (moved / "references" / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_moving_aside_twice_in_one_second_does_not_overwrite(tmp_path):
    local, archive = tmp_path / "skills", tmp_path / "arch"
    first = ordo_skills.move_aside(_skill(local, "c/comfyui", "comfyui"), local, archive, "T")
    second = ordo_skills.move_aside(_skill(local, "c/comfyui", "comfyui"), local, archive, "T")
    assert first != second and first.exists() and second.exists()


def test_one_failed_move_does_not_stop_the_others(tmp_path, monkeypatch):
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    _skill(shipped, "creative/comfyui", "comfyui")
    first = _skill(home / "skills", "a/comfyui", "comfyui")
    second = _skill(home / "skills", "b/comfyui", "comfyui")
    monkeypatch.setattr(ordo_skills, "SHIPPED_ROOT", shipped)
    monkeypatch.setattr(ordo_skills, "register_shipped_root", lambda: None)
    real_move = ordo_skills.move_aside

    def move_but_fail_on_first(skill_dir, *args):
        if skill_dir == first:
            raise OSError("simulated: another container moved it first")
        return real_move(skill_dir, *args)

    monkeypatch.setattr(ordo_skills, "move_aside", move_but_fail_on_first)
    ordo_skills._boot_locked(home)
    assert first.exists() and not second.exists()


@pytest.mark.skipif(importlib.util.find_spec("fcntl") is None, reason="fcntl locking is POSIX-only")
def test_boot_takes_the_shared_lock_and_does_the_work(tmp_path, monkeypatch):
    """Two containers run boot against one home volume; the lock serialises them."""
    shipped, home = tmp_path / "shipped", tmp_path / "home"
    _skill(shipped, "creative/comfyui", "comfyui")
    shadow = _skill(home / "skills", "creative/comfyui", "comfyui")
    monkeypatch.setattr(ordo_skills, "SHIPPED_ROOT", shipped)
    monkeypatch.setattr(ordo_skills, "register_shipped_root", lambda: None)
    monkeypatch.setenv("HERMES_HOME", str(home))
    ordo_skills.boot()
    assert (home / ".ordo-skills.lock").exists()
    assert not shadow.exists()
    assert list((home / "ordo-shadowed-skills").glob("*/creative/comfyui"))


def test_unreadable_frontmatter_is_not_a_match(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: [unterminated\n---\n", encoding="utf-8")
    assert ordo_skills.frontmatter_name(d / "SKILL.md") is None


# --- the overlay itself ---

def test_the_diff_makes_the_scripts_follow_comfyui_url_with_the_stock_fallback():
    diff = (OVERLAY / "submit-via-comfyui-url.diff").read_text(encoding="utf-8")
    assert '+DEFAULT_LOCAL_HOST = os.environ.get("COMFYUI_URL") or "http://127.0.0.1:8188"' in diff
    assert '-DEFAULT_LOCAL_HOST = "http://127.0.0.1:8188"' in diff
    assert "b/skills/creative/comfyui/scripts/_common.py" in diff


def test_the_deployment_notes_forbid_the_engine_address_and_name_the_variable():
    notes = (OVERLAY / "ordo-deployment.md").read_text(encoding="utf-8")
    assert notes.startswith("## ")  # it is spliced in as a section
    assert "$COMFYUI_URL" in notes
    assert "never to `comfyui:8188`" in notes
    assert "Never install or launch ComfyUI" in notes


PERSONAL_PATTERNS = {
    "an email address": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "an IPv4 address": r"\b\d{1,3}(?:\.\d{1,3}){3}\b(?!:8188)",
    "a tailnet name": r"\.ts\.net\b|tail[0-9a-f]{6}",
    # A lone drive letter: "http://" must not count as the drive "p:/".
    "a host filesystem path": r"(?<!\w)[A-Za-z]:[\\/]|/c/dev\b|/home/(?!hermes\b)",
    "a dated incident": r"\b20\d\d-\d\d-\d\d\b",
    "a discord id": r"\b\d{17,20}\b",
    "a token-looking string": r"\b(?:sk|ghp|gho|hf)_[A-Za-z0-9]{16,}\b",
}


@pytest.mark.parametrize("path", sorted(p for p in OVERLAY.rglob("*") if p.is_file()), ids=lambda p: p.name)
@pytest.mark.parametrize("label", sorted(PERSONAL_PATTERNS))
def test_overlay_files_carry_nothing_deployment_specific(path, label):
    """This repo is public. The overlay describes the stack, never one operator's copy of it."""
    text = path.read_text(encoding="utf-8")
    added = "\n".join(line[1:] for line in text.splitlines() if line.startswith("+")) \
        if path.suffix == ".diff" else text
    match = re.search(PERSONAL_PATTERNS[label], added)
    assert not match, f"{path.name} contains {label}: {match.group(0)!r}"
