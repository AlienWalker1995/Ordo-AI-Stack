"""E6: rag-ingestion's hidden-path exclusion (the mechanism the eval scratch folder now relies on
to stay out of RAG) and the known deletion-propagation gap it does NOT close (see
services/rag/README.md's "Known limitation" and services/evals/ordo_evals/runner._check_rag_leak,
the harness's own safety net for that gap)."""
from __future__ import annotations

import ingest


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


def test_no_on_deleted_handler_exists_yet_deletions_are_not_propagated_to_qdrant():
    """Documents the current limitation (services/rag/README.md): there is no code path that removes
    a Qdrant point when its source file disappears from the watch tree. `_delete_existing` only runs
    inside `_upsert_points`, i.e. on a RE-ingest of the SAME source, never on absence. If this class
    ever grows an `on_deleted` handler, update the README's limitation note and this test together."""
    assert "on_deleted" not in vars(ingest._EventHandler)
