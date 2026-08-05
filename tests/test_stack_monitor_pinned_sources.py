"""stack_monitor pinned-upstream-source discovery.

Regression coverage for the 2026-08-05 miss: a locally-built image (LOCAL_PREFIXES)
only ever grades REBUILD/"rebuild if source changed", and nothing looked at the
upstream repo its Dockerfile clones at a fixed SHA. services/hermes/Dockerfile sat
on a 2026-05-19 commit while upstream shipped 11 releases (several citing security
fixes) and every audit reported `agent` as REBUILD and said nothing.

Discovery is by CONVENTION so future pinned builds are covered without editing the
script: `ARG <X>_PINNED_SHA=<sha>` alongside `ARG <X>_REPO=<github url>`. These tests
pin that contract — they are filesystem-only and make no network calls.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_pinned_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def _mkservice(root: Path, name: str, dockerfile: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text(dockerfile, encoding="utf-8")


def test_discovers_pin_and_maps_repo(tmp_path, monkeypatch):
    _mkservice(tmp_path, "hermes", (
        "ARG HERMES_PINNED_SHA=5fdcfd851f7e693fb72a9ea6a6ef25142e63259e\n"
        "ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent.git\n"
        "FROM python:3.11-slim\n"
    ))
    monkeypatch.setattr(sm, "SERVICES_DIR", tmp_path)
    found = sm.discover_pinned_sources()
    assert len(found) == 1
    assert found[0]["service"] == "hermes"
    assert found[0]["gh"] == "NousResearch/hermes-agent"      # .git stripped
    assert found[0]["sha"].startswith("5fdcfd85")


def test_ignores_pin_without_a_resolvable_repo(tmp_path, monkeypatch):
    # A pin whose sibling _REPO is missing or not GitHub cannot be resolved, and
    # must be skipped rather than reported as UNKNOWN noise every single run.
    _mkservice(tmp_path, "orphan", "ARG ORPHAN_PINNED_SHA=abc1234def5678\n")
    _mkservice(tmp_path, "gitlab", (
        "ARG THING_PINNED_SHA=abc1234def5678\n"
        "ARG THING_REPO=https://gitlab.com/someone/thing.git\n"
    ))
    monkeypatch.setattr(sm, "SERVICES_DIR", tmp_path)
    assert sm.discover_pinned_sources() == []


def test_ignores_non_sha_values(tmp_path, monkeypatch):
    # `ARG FOO_PINNED_SHA=v1.2.3` is a tag, not a commit; the compare/commit-date
    # calls assume a SHA, so a non-hex value must not reach them.
    _mkservice(tmp_path, "tagpin", (
        "ARG TAGPIN_PINNED_SHA=v1.2.3\n"
        "ARG TAGPIN_REPO=https://github.com/owner/repo\n"
    ))
    monkeypatch.setattr(sm, "SERVICES_DIR", tmp_path)
    assert sm.discover_pinned_sources() == []


def test_multiple_services_are_all_discovered(tmp_path, monkeypatch):
    _mkservice(tmp_path, "one", (
        "ARG ONE_PINNED_SHA=1111111111111111111111111111111111111111\n"
        "ARG ONE_REPO=https://github.com/o/one\n"
    ))
    _mkservice(tmp_path, "two", (
        "ARG TWO_PINNED_SHA=2222222222222222222222222222222222222222\n"
        "ARG TWO_REPO=https://github.com/o/two.git\n"
    ))
    monkeypatch.setattr(sm, "SERVICES_DIR", tmp_path)
    got = {e["service"]: e["gh"] for e in sm.discover_pinned_sources()}
    assert got == {"one": "o/one", "two": "o/two"}


def test_missing_services_dir_is_not_fatal(tmp_path, monkeypatch):
    # The cron runs from a container where services/ may not be mounted; the audit
    # must degrade to "no pinned sources", never raise and kill the whole report.
    monkeypatch.setattr(sm, "SERVICES_DIR", tmp_path / "does-not-exist")
    assert sm.discover_pinned_sources() == []


def test_real_repo_pins_hermes_upstream():
    # Guards the actual convention in-tree: if services/hermes/Dockerfile stops
    # declaring the pin the way the audit expects, upstream drift goes blind again.
    found = {e["service"]: e for e in sm.discover_pinned_sources()}
    if not found:                      # services/ absent (packaged checkout) — skip
        return
    assert "hermes" in found
    assert found["hermes"]["gh"] == "NousResearch/hermes-agent"
