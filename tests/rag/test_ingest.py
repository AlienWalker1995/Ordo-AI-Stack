"""E6: rag-ingestion's hidden-path exclusion (the mechanism the eval scratch folder now relies on
to stay out of RAG), and how the index follows deleted, moved and no-longer-ingested files (see
services/rag/README.md; services/evals/ordo_evals/runner._check_rag_leak stays as the harness's own
safety net)."""
from __future__ import annotations

import types

import ingest
import pytest


def test_is_hidden_excludes_a_dot_prefixed_folder_at_any_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    assert ingest._is_hidden(tmp_path / ".ordo-scratch" / "run-1" / "note.md")
    assert ingest._is_hidden(tmp_path / "memory-vault" / ".ordo-scratch" / "note.md")
    assert not ingest._is_hidden(tmp_path / "memory-vault" / "notes" / "note.md")


def test_iter_supported_files_skips_the_hidden_scratch_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    scratch = tmp_path / ".ordo-scratch" / "run-1"
    scratch.mkdir(parents=True)
    (scratch / "note.md").write_text("eval-token abc123\n", encoding="utf-8")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "keep.md").write_text("real content\n", encoding="utf-8")

    files = ingest._iter_supported_files()

    assert [f.name for f in files] == ["keep.md"]


def test_ingest_path_refuses_a_hidden_file_even_called_directly(tmp_path, monkeypatch):
    """The watchdog event path calls `ingest_path()` per file, not `_iter_supported_files()`; the
    hidden-path guard must be enforced there too so a create/modify event for a scratch note is
    never embedded even though it was never in the periodic scan's queue."""
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    scratch = tmp_path / ".ordo-scratch" / "run-1"
    scratch.mkdir(parents=True)
    note = scratch / "note.md"
    note.write_text("eval-token abc123\n", encoding="utf-8")

    assert ingest.ingest_path(note, {}) is False


class _DeleteRecorder:
    """Stands in for httpx.Client and records the source of every points/delete request."""

    def __init__(self):
        self.deleted = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, params=None, headers=None):
        assert url.endswith("/points/delete")
        self.deleted.append(json["filter"]["must"][0]["match"]["value"])
        return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None)


@pytest.fixture()
def deletes(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    recorder = _DeleteRecorder()
    monkeypatch.setattr(ingest.httpx, "Client", recorder)
    return recorder


def _write(root, rel):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("text", encoding="utf-8")
    return path


def test_reconcile_deletes_the_chunks_of_a_deleted_file(tmp_path, deletes):
    _write(tmp_path, "memory-vault/notes/keep.md")
    _write(tmp_path, "memory-vault/notes/also.md")
    state = {"memory-vault/notes/keep.md": "1", "memory-vault/notes/also.md": "2",
             "memory-vault/notes/gone.md": "3"}
    seen = {"memory-vault/notes/gone.md": (1, 1)}

    assert ingest.reconcile_index(state, {}, seen) == 1

    assert deletes.deleted == ["memory-vault/notes/gone.md"]
    assert "memory-vault/notes/gone.md" not in state and "memory-vault/notes/gone.md" not in seen


def test_reconcile_covers_sources_still_waiting_for_a_metadata_update(tmp_path, deletes):
    _write(tmp_path, "memory-vault/notes/a.md")
    _write(tmp_path, "memory-vault/notes/b.md")
    state = {"memory-vault/notes/a.md": "1", "memory-vault/notes/b.md": "2"}
    stale = {"memory-vault/notes/gone.md": "3"}

    assert ingest.reconcile_index(state, stale, {}) == 1

    assert deletes.deleted == ["memory-vault/notes/gone.md"] and stale == {}


def test_reconcile_purges_sources_under_an_excluded_prefix(tmp_path, deletes):
    _write(tmp_path, "memory-vault/notes/a.md")
    _write(tmp_path, "memory-vault/notes/b.md")
    _write(tmp_path, "memory-vault/agents/hermes/skill.md")     # on disk, but never ingested
    state = {"memory-vault/notes/a.md": "1", "memory-vault/notes/b.md": "2",
             "memory-vault/agents/hermes/skill.md": "3"}

    ingest.reconcile_index(state, {}, {})

    assert deletes.deleted == ["memory-vault/agents/hermes/skill.md"]


def test_reconcile_purges_hidden_and_unsupported_files(tmp_path, deletes):
    for rel in ("notes/a.md", "notes/b.md", "notes/c.md", "notes/.trash/old.md", "notes/photo.png"):
        _write(tmp_path, rel)
    state = {"notes/a.md": "1", "notes/b.md": "2", "notes/c.md": "3",
             "notes/.trash/old.md": "4", "notes/photo.png": "5"}

    ingest.reconcile_index(state, {}, {})

    assert sorted(deletes.deleted) == ["notes/.trash/old.md", "notes/photo.png"]


def test_a_moved_file_is_deleted_under_its_old_path_and_queued_under_the_new(tmp_path, deletes):
    new = _write(tmp_path, "notes/new.md")
    old = tmp_path / "notes" / "old.md"
    queue, removed = set(), set()
    handler = ingest._EventHandler(queue, ingest.threading.Lock(), removed)

    handler.on_moved(types.SimpleNamespace(is_directory=False, src_path=str(old), dest_path=str(new)))
    state = {"notes/old.md": "1"}
    ingest.remove_gone_files(list(removed), state, {}, {})

    assert queue == {new}
    assert deletes.deleted == ["notes/old.md"] and state == {}


def test_a_delete_event_removes_the_source_unless_the_file_is_back(tmp_path, deletes):
    back = _write(tmp_path, "notes/back.md")
    gone = tmp_path / "notes" / "gone.md"
    state = {"notes/back.md": "1", "notes/gone.md": "2"}
    removed = set()
    handler = ingest._EventHandler(set(), ingest.threading.Lock(), removed)

    for path in (back, gone):
        handler.on_deleted(types.SimpleNamespace(is_directory=False, src_path=str(path)))
    ingest.remove_gone_files(sorted(removed), state, {}, {})

    assert deletes.deleted == ["notes/gone.md"] and list(state) == ["notes/back.md"]


def test_reconcile_never_deletes_from_a_root_with_no_files_on_disk(tmp_path, deletes, caplog):
    _write(tmp_path, "stack-docs/a.md")
    state = {"stack-docs/a.md": "1", "memory-vault/notes/a.md": "2", "memory-vault/notes/b.md": "3"}

    assert ingest.reconcile_index(state, {}, {}) == 0       # memory-vault is missing: a mount not ready

    assert deletes.deleted == [] and "memory-vault/notes/a.md" in state
    assert "skipped root memory-vault" in caplog.text


def test_reconcile_never_deletes_anything_from_an_empty_watch_tree(tmp_path, deletes):
    state = {"notes/a.md": "1", "b.md": "2"}

    assert ingest.reconcile_index(state, {}, {}) == 0
    assert deletes.deleted == [] and len(state) == 2


def test_reconcile_refuses_to_delete_over_the_configured_fraction(tmp_path, deletes, monkeypatch, caplog):
    _write(tmp_path, "notes/a.md")
    state = {"notes/a.md": "1", "notes/x.md": "2", "notes/y.md": "3"}     # 2 of 3 are orphans

    assert ingest.reconcile_index(state, {}, {}) == 0

    assert deletes.deleted == [] and len(state) == 3
    assert "REFUSED" in caplog.text
    monkeypatch.setattr(ingest, "RECONCILE_ALLOW_MASS_DELETE", True)
    assert ingest.reconcile_index(state, {}, {}) == 2
    assert sorted(deletes.deleted) == ["notes/x.md", "notes/y.md"]


def test_reconcile_deletes_up_to_the_fraction(tmp_path, deletes):
    _write(tmp_path, "notes/a.md")
    state = {"notes/a.md": "1", "notes/x.md": "2"}                          # exactly 50%

    assert ingest.reconcile_index(state, {}, {}) == 1
