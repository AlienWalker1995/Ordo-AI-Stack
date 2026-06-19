"""storage_purge retention-policy unit tests.

The deletion logic lives in small pure functions so policy can be tested without
touching the filesystem. The impure shell (walking dirs, unlinking) is kept thin
and exercised only via a temp tree in the apply test.
"""
from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "storage_purge.py"
_spec = importlib.util.spec_from_file_location("storage_purge_under_test", _PATH)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

NOW = datetime(2026, 6, 19, tzinfo=UTC)


# --- draft_expired: age from the YYYY-MM-DD folder-name prefix ---

def test_draft_well_past_window_is_expired():
    assert sp.draft_expired("2026-01-01_old_news", NOW, max_age_days=60) is True


def test_draft_inside_window_is_kept():
    assert sp.draft_expired("2026-06-10_recent", NOW, max_age_days=60) is False


def test_draft_exactly_at_boundary_is_kept():
    # 60 days old exactly -> not yet "older than 60 days".
    assert sp.draft_expired("2026-04-20_edge", NOW, max_age_days=60) is False


def test_draft_one_day_past_boundary_is_expired():
    assert sp.draft_expired("2026-04-19_edge", NOW, max_age_days=60) is True


def test_unparseable_draft_name_is_never_expired():
    # Never delete something we cannot confidently date.
    assert sp.draft_expired("no-date-here", NOW, max_age_days=60) is False
    assert sp.draft_expired("2026-13-99_bad_date", NOW, max_age_days=60) is False


# --- is_older_than: mtime-based aging for output/input ---

def test_is_older_than_true_when_past():
    now = NOW.timestamp()
    old = now - 22 * 86400
    assert sp.is_older_than(old, now, 21) is True


def test_is_older_than_false_when_recent():
    now = NOW.timestamp()
    recent = now - 5 * 86400
    assert sp.is_older_than(recent, now, 21) is False


# --- backups_to_delete: keep newest N by sortable timestamp name ---

def test_backups_keep_newest_three():
    names = ["20260101-000000", "20260201-000000", "20260301-000000",
             "20260401-000000", "20260501-000000"]
    to_delete = sp.backups_to_delete(names, keep=3)
    assert sorted(to_delete) == ["20260101-000000", "20260201-000000"]


def test_backups_fewer_than_keep_deletes_nothing():
    assert sp.backups_to_delete(["20260101-000000", "20260201-000000"], keep=3) == []


def test_backups_exactly_keep_deletes_nothing():
    names = ["20260101-000000", "20260201-000000", "20260301-000000"]
    assert sp.backups_to_delete(names, keep=3) == []


# --- exceeds_cap: safety guard against runaway deletion ---

def test_cap_not_exceeded_under_both_limits():
    assert sp.exceeds_cap(del_items=2, bucket_items=100, del_bytes=1_000_000) is False


def test_cap_exceeded_by_fraction():
    assert sp.exceeds_cap(del_items=60, bucket_items=100, del_bytes=1, max_frac=0.5) is True


def test_cap_exceeded_by_bytes():
    big = 11 * 1024 ** 3
    assert sp.exceeds_cap(del_items=1, bucket_items=100, del_bytes=big) is True


def test_cap_empty_bucket_does_not_divide_by_zero():
    assert sp.exceeds_cap(del_items=0, bucket_items=0, del_bytes=0) is False


# --- within_root: deletions confined to data/ ---

def test_within_root_true_for_child(tmp_path):
    root = tmp_path / "data"
    (root / "drafts").mkdir(parents=True)
    assert sp.within_root(root / "drafts", root) is True


def test_within_root_false_for_outside(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "etc"
    outside.mkdir()
    assert sp.within_root(outside, root) is False


def test_within_root_false_for_parent_traversal(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    assert sp.within_root(root / ".." / "secret", root) is False


# --- plan_purge / execute_purge: integration against a temp data tree ---


def _aged_file(path, days_old, now_epoch, size=1024):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = now_epoch - days_old * 86400
    os.utime(path, (ts, ts))


def _build_tree(root, now):
    now_epoch = now.timestamp()
    # drafts: one expired (>60d by name), one recent
    (root / "drafts" / "2026-01-01_old" ).mkdir(parents=True)
    (root / "drafts" / "2026-01-01_old" / "video.mp4").write_bytes(b"x" * 2048)
    (root / "drafts" / "2026-06-10_new").mkdir(parents=True)
    (root / "drafts" / "2026-06-10_new" / "video.mp4").write_bytes(b"x" * 2048)
    # comfyui-output: one old file, one recent
    _aged_file(root / "comfyui-output" / "old.png", 30, now_epoch)
    _aged_file(root / "comfyui-output" / "new.png", 2, now_epoch)
    # comfyui input: one old, one recent
    cin = root / "comfyui-storage" / "ComfyUI" / "input"
    _aged_file(cin / "old_input.png", 40, now_epoch)
    _aged_file(cin / "fresh_input.png", 3, now_epoch)
    # backups: 5 timestamped dirs
    for stamp in ["20260101-000000", "20260201-000000", "20260301-000000",
                  "20260401-000000", "20260501-000000"]:
        (root / "_backups" / stamp).mkdir(parents=True)
        (root / "_backups" / stamp / "jobs.json").write_bytes(b"{}")


def test_plan_does_not_delete_anything(tmp_path):
    root = tmp_path / "data"
    _build_tree(root, NOW)
    sp.plan_purge(root, NOW)
    # Everything still present after planning.
    assert (root / "drafts" / "2026-01-01_old").exists()
    assert (root / "comfyui-output" / "old.png").exists()


def test_execute_removes_expired_drafts_keeps_recent(tmp_path):
    root = tmp_path / "data"
    _build_tree(root, NOW)
    plan = sp.plan_purge(root, NOW)
    sp.execute_purge(plan, root)
    assert not (root / "drafts" / "2026-01-01_old").exists()
    assert (root / "drafts" / "2026-06-10_new").exists()


def test_execute_removes_old_comfyui_files_keeps_recent(tmp_path):
    root = tmp_path / "data"
    _build_tree(root, NOW)
    plan = sp.plan_purge(root, NOW)
    sp.execute_purge(plan, root)
    assert not (root / "comfyui-output" / "old.png").exists()
    assert (root / "comfyui-output" / "new.png").exists()
    assert not (root / "comfyui-storage" / "ComfyUI" / "input" / "old_input.png").exists()
    assert (root / "comfyui-storage" / "ComfyUI" / "input" / "fresh_input.png").exists()


def test_execute_keeps_newest_three_backups(tmp_path):
    root = tmp_path / "data"
    _build_tree(root, NOW)
    plan = sp.plan_purge(root, NOW)
    sp.execute_purge(plan, root)
    remaining = sorted(p.name for p in (root / "_backups").iterdir())
    assert remaining == ["20260301-000000", "20260401-000000", "20260501-000000"]


def test_cap_guard_aborts_bucket_without_force(tmp_path):
    # A tree where ALL drafts are expired -> 100% of bucket -> cap trips.
    root = tmp_path / "data"
    (root / "drafts" / "2026-01-01_a").mkdir(parents=True)
    (root / "drafts" / "2026-01-01_a" / "v.mp4").write_bytes(b"x")
    (root / "drafts" / "2026-01-02_b").mkdir(parents=True)
    (root / "drafts" / "2026-01-02_b" / "v.mp4").write_bytes(b"x")
    plan = sp.plan_purge(root, NOW)
    result = sp.execute_purge(plan, root, force=False)
    assert result["drafts"]["aborted"] is True
    assert (root / "drafts" / "2026-01-01_a").exists()  # nothing deleted


def test_cap_guard_bypassed_with_force(tmp_path):
    root = tmp_path / "data"
    (root / "drafts" / "2026-01-01_a").mkdir(parents=True)
    (root / "drafts" / "2026-01-01_a" / "v.mp4").write_bytes(b"x")
    (root / "drafts" / "2026-01-02_b").mkdir(parents=True)
    (root / "drafts" / "2026-01-02_b" / "v.mp4").write_bytes(b"x")
    plan = sp.plan_purge(root, NOW)
    result = sp.execute_purge(plan, root, force=True)
    assert result["drafts"]["aborted"] is False
    assert not (root / "drafts" / "2026-01-01_a").exists()
