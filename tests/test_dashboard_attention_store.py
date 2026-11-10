"""The Overview's dismissals file: a missing, corrupt or wrongly shaped file reads as no
dismissals instead of breaking the page, and a write replaces the file whole."""
from __future__ import annotations

import json

from dashboard import attention_store


def test_a_missing_file_is_no_dismissals(tmp_path):
    assert attention_store.load(tmp_path / "absent.json") == {}


def test_a_corrupt_file_is_no_dismissals_and_the_next_write_replaces_it(tmp_path):
    path = tmp_path / attention_store.FILE_NAME
    path.write_text("{not json", encoding="utf-8")
    assert attention_store.load(path) == {}
    attention_store.save(path, {"disk": 5.0})
    assert json.loads(path.read_text(encoding="utf-8")) == {"disk": 5.0}


def test_a_file_that_is_not_an_object_is_no_dismissals(tmp_path):
    path = tmp_path / attention_store.FILE_NAME
    path.write_text('["disk"]', encoding="utf-8")
    assert attention_store.load(path) == {}


def test_entries_without_a_numeric_time_are_dropped(tmp_path):
    path = tmp_path / attention_store.FILE_NAME
    path.write_text(json.dumps({"disk": 1, "memory": "yesterday"}), encoding="utf-8")
    assert attention_store.load(path) == {"disk": 1.0}


def test_a_save_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / attention_store.FILE_NAME
    attention_store.save(path, {"disk": 1.0})
    assert sorted(p.name for p in tmp_path.iterdir()) == [attention_store.FILE_NAME]
