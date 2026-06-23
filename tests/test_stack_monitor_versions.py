"""stack_monitor version-resolution + rolling-tag classification.

Regression coverage for the audit fix: services pinned by a rolling tag or built
from source (llama.cpp 'server-cuda', litellm 'main-stable') have no comparable
semver and must be flagged ROLLING for manual review — NOT silently reported as a
MEDIUM "version format unknown" update. ComfyUI/LiteLLM current versions are read
from their real source instead of a stale hardcode.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_versions_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def test_rolling_tag_is_rolling_not_medium():
    sev, msg = sm.classify_severity("server-cuda", "b4567", "ordinary release notes")
    assert sev == "ROLLING"
    assert "rebuild" in msg.lower()


def test_built_image_tag_is_rolling():
    sev, _ = sm.classify_severity("main-stable", "v1.89.2", "ordinary release notes")
    assert sev == "ROLLING"


def test_security_beats_rolling():
    # A CVE in the latest notes must win even when current is a rolling tag.
    sev, _ = sm.classify_severity("server-cuda", "b1", "fixes CVE-2026-1234 buffer overflow")
    assert sev == "CRITICAL"


def test_real_semver_minor_update():
    # ComfyUI 0.17.0 -> 0.25.1 is a genuine, comparable update.
    sev, _ = sm.classify_severity("0.17.0", "v0.25.1", "minor changes")
    assert sev == "MEDIUM"


def test_real_semver_already_current():
    sev, _ = sm.classify_severity("1.89.2", "v1.89.2", "no change")
    assert sev == "SAFE"


def test_comfyui_resolves_from_version_file_when_present():
    # Only assert when the file exists in this checkout (it does in the live repo).
    if sm.COMFYUI_VERSION_FILE.exists():
        assert sm.read_comfyui_version() == sm.resolve_current_version("ComfyUI", {})


def test_unknown_current_falls_back_to_rolling_not_crash():
    sev, _ = sm.classify_severity("unknown", "v1.2.3", "notes")
    assert sev == "ROLLING"
