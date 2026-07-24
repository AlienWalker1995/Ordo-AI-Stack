"""stack_monitor version-resolution + rolling-tag classification.

Regression coverage for the audit fix: services pinned by a rolling tag or built
from source (llama.cpp 'server-cuda', litellm 'main-stable') have no comparable
semver and must be flagged DRIFT for manual review — NOT silently reported as a
diffable semver update. Security language in upstream release notes is only
surfaced as SECURITY when the pin is actually diffable (semver/digest); a
rolling pin is always DRIFT regardless of what the notes say, since the
recommendation for an unreproducible tag is "flag it every run," not "read the
CVE." ComfyUI's real upstream version is tracked via the HINTS "upstream"
mapping (github_latest against comfy-org/ComfyUI) instead of a stale hardcode.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_versions_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def test_rolling_tag_is_drift_not_update():
    kind = sm.classify("ghcr.io/ggml-org/llama.cpp:server-cuda")["kind"]
    assert kind == "rolling"
    tier, reason = sm.severity(kind, "server-cuda", "b4567", "ordinary release notes")
    assert tier == "DRIFT"
    assert "rolling" in reason.lower()


def test_built_image_tag_is_rolling():
    kind = sm.classify("ghcr.io/berriai/litellm:main-stable")["kind"]
    assert kind == "rolling"
    tier, _ = sm.severity(kind, "main-stable", "v1.89.2", "ordinary release notes")
    assert tier == "DRIFT"


def test_rolling_tier_holds_even_with_security_language_in_body():
    # A CVE mentioned in the latest upstream notes does not upgrade a rolling
    # pin's tier — it's DRIFT either way, by design (see module docstring:
    # "rolling -> flag as drift every run").
    kind = sm.classify("ghcr.io/ggml-org/llama.cpp:server-cuda")["kind"]
    tier, _ = sm.severity(kind, "server-cuda", "b1", "fixes CVE-2026-1234 buffer overflow")
    assert tier == "DRIFT"


def test_security_tier_for_diffable_bump_with_cve_body():
    # SECURITY only fires for a pin kind that's actually diffable (semver/digest).
    tier, reason = sm.severity("semver", "1.0.0", "1.0.1", "fixes CVE-2026-1234 buffer overflow")
    assert tier == "SECURITY"
    assert "1.0.1" in reason


def test_real_semver_minor_update():
    # ComfyUI 0.17.0 -> 0.25.1 is a genuine, comparable update.
    tier, reason = sm.severity("semver", "0.17.0", "v0.25.1", "minor changes")
    assert tier == "UPDATE"
    assert "minor" in reason


def test_real_semver_already_current():
    tier, reason = sm.severity("semver", "1.89.2", "v1.89.2", "no change")
    assert tier == "OK"
    assert "1.89.2" in reason


def test_comfyui_boot_tracks_upstream_project_not_hardcoded():
    # yanwk/comfyui-boot is a boot wrapper; its real version comes from the
    # ComfyUI project it tracks, via HINTS "upstream", not a baked-in constant.
    hint = sm.hint_for("yanwk/comfyui-boot")
    assert hint.get("upstream") == ("ComfyUI", "comfy-org/ComfyUI")


def test_unknown_current_falls_back_to_rolling_not_crash():
    kind = sm.classify("ghcr.io/foo/bar:unknown")["kind"]
    assert kind == "rolling"
    tier, _ = sm.severity(kind, "unknown", "v1.2.3", "notes")
    assert tier == "DRIFT"
