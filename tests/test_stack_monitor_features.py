"""stack_monitor new-feature collection for the weekly digest.

The weekly digest reports the major features of upstream releases the deployed pin is
missing, across Ordo and the extra stacks named in STACK_AUDIT_SOURCES. These tests pin
the parts that decide what reaches the model: which releases count as missed, which
release is the latest, which release-note lines count as features, and how a broken or
partial sources file degrades.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "stack_monitor.py"
_spec = importlib.util.spec_from_file_location("stack_monitor_features_under_test", _PATH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

NOW = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)


def _release(tag, published="2026-09-01", body=""):
    return {"tag_name": tag, "published_at": f"{published}T00:00:00Z", "html_url": f"u/{tag}", "body": body}


def test_feature_section_bullets_are_kept_and_fix_sections_dropped():
    body = """## Features
- Add bearer authentication to the widget by @someone in https://github.com/o/r/pull/12
- **New** search providers (#4521)

## Bug Fixes
- Fixed a crash on startup

## New Contributors
- @someone made their first contribution
"""
    assert sm.feature_lines(body) == [
        "Add bearer authentication to the widget",
        "New search providers",
    ]


def test_unsectioned_notes_keep_only_feature_looking_lines():
    # Sonarr/Radarr style: one generic section, features marked "New:", fixes "Fixed:".
    body = """## What's Changed
### Changes
* New: Filter movies by movie file quality by @dev in https://github.com/Radarr/Radarr/pull/1
* Fixed: Avoid checking for free space by @dev in https://github.com/Radarr/Radarr/pull/2
"""
    assert sm.feature_lines(body) == ["New: Filter movies by movie file quality"]


def test_fix_only_release_yields_no_features():
    body = "## What's Changed\n* Fix Users table sort logic by @dev in https://x/pull/1\n"
    assert sm.feature_lines(body) == []


def test_commit_hashes_and_html_entities_are_cleaned():
    body = "- 7448b2a Added links for #&lt;number&gt; patterns 02bae52\n- (api) Add ntfy tags - (92bad10)\n"
    assert sm.feature_lines(body) == ["Added links for #<number> patterns"]


def test_newest_release_is_highest_version_not_first_listed():
    # n8n lists a `stable` release first; ClickHouse publishes LTS backports after newer lines.
    releases = [_release("stable"), _release("n8n@2.41.5"), _release("n8n@2.41.6")]
    assert sm.newest_release(releases)["tag_name"] == "n8n@2.41.6"
    assert sm.newest_release([_release("stable")])["tag_name"] == "stable"


def test_missed_releases_for_a_version_pin_are_those_newer_than_the_pin():
    releases = [_release("v3.5.0"), _release("v3.4.1"), _release("v3.4.0")]
    missed = sm.missed_releases(releases, "v3.4.1", "semver", NOW)
    assert [r["tag_name"] for r in missed] == ["v3.5.0"]


def test_missed_releases_for_a_pin_without_version_are_this_weeks():
    releases = [_release("v13.2.3", "2026-10-02"), _release("v13.2.2", "2026-09-20")]
    missed = sm.missed_releases(releases, "", "digest", NOW)
    assert [r["tag_name"] for r in missed] == ["v13.2.3"]


def test_release_features_flags_new_this_week_and_summarises_prose_notes():
    prose = "# Release v2\n\nThis window adds a plugin SDK for desktop clients and faster sync.\n"
    rows = sm.release_features([_release("v2", "2026-10-01", prose), _release("v1", "2026-09-01")], NOW)
    assert rows[0]["new_this_week"] is True and rows[1]["new_this_week"] is False
    assert rows[0]["features"] == []
    assert rows[0]["summary"] == ["This window adds a plugin SDK for desktop clients and faster sync."]


def test_sources_file_adds_stacks_after_ordo(tmp_path, monkeypatch):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"stacks": [{"name": "media", "compose": "/x/compose.yaml"}]}))
    monkeypatch.setattr(sm, "SOURCES_FILE", str(sources))
    stacks, errors = sm.load_sources()
    assert [s["stack"] for s in stacks] == ["ordo", "media"]
    assert stacks[1]["env"] is None
    assert errors == []


def test_broken_sources_file_is_reported_and_ordo_still_audited(tmp_path, monkeypatch):
    sources = tmp_path / "sources.json"
    sources.write_text("{not json")
    monkeypatch.setattr(sm, "SOURCES_FILE", str(sources))
    stacks, errors = sm.load_sources()
    assert [s["stack"] for s in stacks] == ["ordo"]
    assert len(errors) == 1 and "sources file" in errors[0]


def test_no_sources_file_means_ordo_only(monkeypatch):
    monkeypatch.setattr(sm, "SOURCES_FILE", "")
    stacks, errors = sm.load_sources()
    assert [s["stack"] for s in stacks] == ["ordo"] and errors == []
