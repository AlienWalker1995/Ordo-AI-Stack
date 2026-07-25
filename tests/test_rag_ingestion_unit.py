"""Unit tests for rag-ingestion chunking and helpers (no Docker)."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _stub_watchdog() -> None:
    """ingest.py imports watchdog; tests do not install rag-ingestion requirements."""
    if "watchdog.events" in sys.modules:
        return
    events = types.ModuleType("watchdog.events")

    class FileSystemEventHandler:  # noqa: D401
        pass

    events.FileSystemEventHandler = FileSystemEventHandler
    sys.modules["watchdog.events"] = events
    observers = types.ModuleType("watchdog.observers")

    class Observer:  # noqa: D401
        pass

    observers.Observer = Observer
    sys.modules["watchdog.observers"] = observers
    sys.modules["watchdog"] = types.ModuleType("watchdog")


def _load_ingest():
    _stub_watchdog()
    path = Path(__file__).resolve().parent.parent / "services" / "rag" / "ingest.py"
    spec = importlib.util.spec_from_file_location("rag_ingest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rag_ingest"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_chunk_splits_words_with_overlap():
    ingest = _load_ingest()
    # size/overlap are in words (see CHUNK_SIZE in ingest)
    chunks = ingest._chunk("one two three four five", size=2, overlap=0)
    assert chunks == ["one two", "three four", "five"]

    chunks_ov = ingest._chunk("a b c d e f", size=3, overlap=1)
    assert len(chunks_ov) >= 2
    assert chunks_ov[0] == "a b c"
    # step is size - overlap = 2
    assert chunks_ov[1] == "c d e"


def test_chunk_empty():
    ingest = _load_ingest()
    assert ingest._chunk("", 10, 0) == []
    assert ingest._chunk("   ", 10, 0) == []


def test_file_key_relative():
    ingest = _load_ingest()
    path = Path("/watch/docs/note.md")
    key = ingest._file_key(path)
    assert key.replace("\\", "/") == "docs/note.md"


def test_supported_extensions_include_common_types():
    ingest = _load_ingest()
    assert ".pdf" in ingest.SUPPORTED_EXTENSIONS
    assert ".md" in ingest.SUPPORTED_EXTENSIONS
