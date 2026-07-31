#!/usr/bin/env python3
"""Lightweight watch-and-ingest service for Qdrant-backed RAG."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import httpx

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional for non-PDF ingestion/test environments
    PdfReader = None  # type: ignore[assignment]

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:  # pragma: no cover - polling path still works without watchdog
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]


logging.basicConfig(level=logging.INFO, format="%(asctime)s [rag-ingestion] %(levelname)s %(message)s")
logger = logging.getLogger("rag-ingestion")

# EMBED_URL is the current name (matches its sibling, qdrant-rag-mcp/server.py); MODEL_GATEWAY_URL
# is kept as a fallback for backward compat with plugins/rag/plugin.yaml's existing env, which
# despite the name actually points this service straight at llamacpp-embed, not the LiteLLM gateway.
EMBED_URL = os.environ.get("EMBED_URL", os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435")).rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text-v1.5.Q4_K_M.gguf").strip()
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "documents").strip()
WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/watch")).resolve()
CHUNK_SIZE = max(1, int(os.environ.get("CHUNK_SIZE", "400")))
CHUNK_OVERLAP = max(0, int(os.environ.get("CHUNK_OVERLAP", "50")))
SCAN_INTERVAL_SEC = max(5, int(os.environ.get("SCAN_INTERVAL_SEC", "15")))
HEARTBEAT_PATH = Path("/tmp/rag-ingestion.heartbeat")
STATE_PATH = Path("/tmp/rag-ingestion-state.json")

SUPPORTED_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".json",
    ".csv",
    ".html",
    ".htm",
    ".xml",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".pdf",
}


def _chunk(text: str, size: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = max(1, size - overlap)
    chunks: list[str] = []
    for start in range(0, len(words), step):
        window = words[start : start + size]
        if not window:
            break
        chunks.append(" ".join(window))
        if start + size >= len(words):
            break
    return chunks


def _file_key(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WATCH_DIR)).replace("\\", "/")
    except (ValueError, OSError):
        return path.name


def _is_hidden(path: Path) -> bool:
    """True when any path component under WATCH_DIR starts with a dot.

    Keeps tool/app internals out of the index — e.g. an Obsidian vault's
    ``.obsidian/`` (workspace JSON) and ``.trash/`` (soft-deleted notes), or a
    stray ``.git/``. Applies to both the periodic scan and watchdog events.
    """
    try:
        rel = path.resolve().relative_to(WATCH_DIR)
    except (ValueError, OSError):
        rel = Path(path.name)
    return any(part.startswith(".") for part in rel.parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Markdown image destinations are retrieval noise (hash filenames carry no meaning) and
# tokenize ~10x denser than prose — a 400-word run of ![alt](hash.png) links reached 13k
# tokens live, overflowing any usable embed context. Keep the alt text (OCR captions are
# real retrieval signal), drop the destination.
IMAGE_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)\s]*\)")


def _strip_image_targets(text: str) -> str:
    return IMAGE_LINK_RE.sub(r"\1", text)


def _read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if PdfReader is None:
            logger.warning("Skipping PDF extraction for %s because pypdf is not installed", path)
            return ""
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _load_state() -> dict[str, str]:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _ensure_collection(vector_size: int) -> None:
    url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}"
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url)
        if r.status_code == 200:
            return
        if r.status_code not in (404,):
            r.raise_for_status()
        create = client.put(
            url,
            json={"vectors": {"size": vector_size, "distance": "Cosine"}},
        )
        create.raise_for_status()


def _embed_one(client: httpx.Client, chunk: str) -> list[float]:
    """Embed a single chunk, halving it on 'too large' rejections so one oversized
    chunk (dense tokens: CJK OCR text, pathological markdown) degrades to a truncated
    embedding instead of failing the whole file forever."""
    text = chunk
    for attempt in range(4):
        r = client.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"model": EMBED_MODEL, "input": [text]},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code < 400:
            items = r.json().get("data", [])
            if items and items[0].get("embedding"):
                return items[0]["embedding"]
        if attempt < 3:
            text = text[: max(1, len(text) // 2)]
            logger.warning(
                "Embed rejected a chunk (%d); retrying truncated to %d chars",
                r.status_code, len(text),
            )
    r.raise_for_status()
    raise RuntimeError("embedding response contained no vector")


def _embed(chunks: list[str]) -> list[list[float]]:
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"model": EMBED_MODEL, "input": chunks},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            # One oversized chunk fails the whole batch — fall back to per-chunk
            # embedding with truncation so the rest of the file still indexes.
            logger.warning("Batch embed failed (%d); retrying per-chunk", r.status_code)
            return [_embed_one(client, c) for c in chunks]
    data = r.json()
    items = data.get("data", [])
    return [item.get("embedding", []) for item in items if item.get("embedding")]


def _delete_existing(client: httpx.Client, source: str) -> None:
    r = client.post(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/delete",
        json={
            "filter": {
                "must": [
                    {"key": "source", "match": {"value": source}},
                ]
            }
        },
    )
    r.raise_for_status()


def _upsert_points(source: str, digest: str, chunks: list[str], vectors: list[list[float]]) -> None:
    if not vectors:
        return
    _ensure_collection(len(vectors[0]))
    points = []
    for idx, (chunk_text, vector) in enumerate(zip(chunks, vectors, strict=False)):
        points.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{idx}:{digest}")),
                "vector": vector,
                "payload": {
                    "source": source,
                    "chunk_index": idx,
                    "content": chunk_text,
                    "chunk_size": CHUNK_SIZE,
                    "chunk_overlap": CHUNK_OVERLAP,
                    "digest": digest,
                },
            }
        )
    with httpx.Client(timeout=60.0) as client:
        _delete_existing(client, source)
        r = client.put(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
            json={"points": points},
        )
        r.raise_for_status()


def _iter_supported_files() -> list[Path]:
    if not WATCH_DIR.is_dir():
        return []
    return sorted(
        [
            path
            for path in WATCH_DIR.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS and not _is_hidden(path)
        ]
    )


def ingest_path(path: Path, state: dict[str, str]) -> bool:
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS or _is_hidden(path):
        return False
    source = _file_key(path)
    digest = _sha256(path)
    if state.get(source) == digest:
        return False
    text = _strip_image_targets(_read_text(path))
    chunks = _chunk(text, CHUNK_SIZE, CHUNK_OVERLAP)
    if not chunks:
        logger.info("Skipping %s: no extractable text", source)
        state[source] = digest
        return False
    vectors = _embed(chunks)
    _upsert_points(source, digest, chunks, vectors)
    state[source] = digest
    logger.info("Ingested %s (%d chunks)", source, len(chunks))
    return True


def _heartbeat(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")
        stop_event.wait(10)


class _EventHandler(FileSystemEventHandler):
    def __init__(self, queue: set[Path], lock: threading.Lock) -> None:
        self._queue = queue
        self._lock = lock

    def on_created(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        with self._lock:
            self._queue.add(Path(event.src_path))

    def on_modified(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return
        with self._lock:
            self._queue.add(Path(event.src_path))


def main() -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    stop_event = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(stop_event,), daemon=True)
    heartbeat.start()

    queue: set[Path] = set(_iter_supported_files())
    queue_lock = threading.Lock()
    observer = None
    if Observer is not None:
        observer = Observer()
        observer.schedule(_EventHandler(queue, queue_lock), str(WATCH_DIR), recursive=True)
        observer.start()

    logger.info("Watching %s for RAG ingestion into collection %s", WATCH_DIR, QDRANT_COLLECTION)
    try:
        while True:
            with queue_lock:
                if not queue:
                    queue.update(_iter_supported_files())
                pending = list(queue)
                queue.clear()
            changed = False
            for path in pending:
                try:
                    changed = ingest_path(path, state) or changed
                except Exception as exc:
                    logger.exception("Failed to ingest %s: %s", path, exc)
            if changed:
                _save_state(state)
            time.sleep(SCAN_INTERVAL_SEC)
    finally:
        stop_event.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)


if __name__ == "__main__":
    main()
