#!/usr/bin/env python3
"""Lightweight watch-and-ingest service for Qdrant-backed RAG."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
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
# despite the name actually points this service straight at inference-engine-embed, not the LiteLLM gateway.
EMBED_URL = os.environ.get("EMBED_URL", os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435")).rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embeddinggemma-2-Q8_0.gguf").strip()
# The embedding model's document prompt, prepended to every chunk before it is embedded (the stored
# `content` stays the raw chunk). Queries need the matching query prompt (EMBED_QUERY_PREFIX in
# services/qdrant-rag/server.py).
EMBED_DOCUMENT_PREFIX = os.environ.get("EMBED_DOCUMENT_PREFIX", "")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "documents").strip()
WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/watch")).resolve()
CHUNK_SIZE = max(1, int(os.environ.get("CHUNK_SIZE", "400")))
CHUNK_OVERLAP = max(0, int(os.environ.get("CHUNK_OVERLAP", "50")))
SCAN_INTERVAL_SEC = max(5, int(os.environ.get("SCAN_INTERVAL_SEC", "15")))
# Chunks per embedding request. The engine computes about 3.4 chunks a second and keeps computing a
# request after its client gives up, so one request must stay far inside EMBED_TIMEOUT_SECONDS.
EMBED_BATCH_SIZE = max(1, int(os.environ.get("EMBED_BATCH_SIZE", "16")))
EMBED_TIMEOUT_SECONDS = 120.0
# How long a file that failed waits before its next attempt: doubles from the first to the cap.
RETRY_FIRST_SECONDS = 60.0
RETRY_MAX_SECONDS = 3600.0
HEARTBEAT_PATH = Path("/tmp/rag-ingestion.heartbeat")

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
    """The file's path under WATCH_DIR. Scans and watch events both name files under the resolved
    WATCH_DIR, so this needs no filesystem access."""
    try:
        return path.relative_to(WATCH_DIR).as_posix()
    except ValueError:
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


def _indexed_digests() -> dict[str, str]:
    """{source: digest} of every file in the collection. The store is the record of what is indexed,
    so a recreated container re-embeds nothing and an emptied store re-embeds everything."""
    digests: dict[str, str] = {}
    request = {"limit": 1000, "with_payload": ["source", "digest"], "with_vector": False}
    with httpx.Client(timeout=30.0) as client:
        while True:
            r = client.post(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll", json=request)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            result = r.json().get("result", {})
            for point in result.get("points", []):
                payload = point.get("payload") or {}
                if payload.get("source") and payload.get("digest"):
                    digests[payload["source"]] = payload["digest"]
            next_offset = result.get("next_page_offset")
            if next_offset is None:
                return digests
            request = {**request, "offset": next_offset}


def _drop_collection_if_other_model() -> bool:
    """Drop the collection when its vectors came from a different embedding model.

    Vectors from two models live in different spaces, so a model swap must re-embed everything;
    mixing them returns nonsense neighbours. Each point records the model that embedded it
    (`embed_model`); points written before that field existed count as another model. Returns True
    when the collection was dropped.
    """
    url = f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{url}/points/scroll", json={"limit": 1, "with_payload": ["embed_model"]})
        if r.status_code == 404:
            return False
        r.raise_for_status()
        points = r.json().get("result", {}).get("points", [])
        if not points:
            return False
        stored_model = (points[0].get("payload") or {}).get("embed_model")
        if stored_model == EMBED_MODEL:
            return False
        logger.warning("Collection %s was embedded with %s, not %s: dropping it to re-embed every file",
                       QDRANT_COLLECTION, stored_model or "an unrecorded model", EMBED_MODEL)
        client.delete(url).raise_for_status()
        return True


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


def _embed_batch(client: httpx.Client, batch: list[str]) -> list[list[float]]:
    r = client.post(
        f"{EMBED_URL}/v1/embeddings",
        json={"model": EMBED_MODEL, "input": batch},
        headers={"Content-Type": "application/json"},
    )
    if r.status_code >= 400:
        # One oversized chunk fails the whole batch — fall back to per-chunk
        # embedding with truncation so the rest of the file still indexes.
        logger.warning("Batch embed failed (%d); retrying per-chunk", r.status_code)
        return [_embed_one(client, c) for c in batch]
    items = r.json().get("data", [])
    vectors = [item.get("embedding", []) for item in items if item.get("embedding")]
    if len(vectors) != len(batch):
        # Storing these would pair chunks with another chunk's vector.
        raise RuntimeError(f"embedding response had {len(vectors)} vectors for {len(batch)} chunks")
    return vectors


def _embed(chunks: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    with httpx.Client(timeout=EMBED_TIMEOUT_SECONDS) as client:
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            vectors.extend(_embed_batch(client, chunks[start : start + EMBED_BATCH_SIZE]))
    return vectors


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
                    "embed_model": EMBED_MODEL,
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
    """Every file to index. Hidden directories are not entered at all, and names alone decide the
    rest: each extra stat here is a round trip over the 9p mount, repeated on every scan."""
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(WATCH_DIR):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            path = Path(root) / name
            if not name.startswith(".") and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(path)
    return sorted(found)


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
    vectors = _embed([EMBED_DOCUMENT_PREFIX + chunk for chunk in chunks])
    _upsert_points(source, digest, chunks, vectors)
    state[source] = digest
    logger.info("Ingested %s (%d chunks)", source, len(chunks))
    return True


def _file_signature(path: Path) -> tuple[int, int] | None:
    """(modified time, size), or None when the file is gone. Telling that a file is unchanged from
    this costs one stat, where hashing it reads the whole file over the 9p mount."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


class RetryBackoff:
    """When each failed file may be tried again. A file that keeps failing is retried less and less
    often, and editing it makes it due at once."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # source -> (signature of the failed version, seconds to wait next, when it is due)
        self._failures: dict[str, tuple[tuple[int, int], float, float]] = {}

    def is_due(self, source: str, signature: tuple[int, int]) -> bool:
        failure = self._failures.get(source)
        return failure is None or failure[0] != signature or self._clock() >= failure[2]

    def record_failure(self, source: str, signature: tuple[int, int]) -> float:
        """Returns how many seconds this file now waits."""
        previous = self._failures.get(source)
        wait = previous[1] if previous and previous[0] == signature else RETRY_FIRST_SECONDS
        self._failures[source] = (signature, min(wait * 2, RETRY_MAX_SECONDS), self._clock() + wait)
        return wait

    def record_success(self, source: str) -> None:
        self._failures.pop(source, None)


def ingest_pending(paths: list[Path], state: dict[str, str], seen: dict[str, tuple[int, int]],
                   backoff: RetryBackoff) -> None:
    """One pass. `seen` holds the signature of each file already handled, so an unchanged file costs
    a stat. One failing file waits its turn without stopping the others."""
    for path in paths:
        signature = _file_signature(path)
        if signature is None:
            continue
        source = _file_key(path)
        if seen.get(source) == signature or not backoff.is_due(source, signature):
            continue
        try:
            ingest_path(path, state)
        except Exception as exc:
            wait = backoff.record_failure(source, signature)
            logger.exception("Failed to ingest %s: %s (next attempt in %.0f s)", path, exc, wait)
        else:
            backoff.record_success(source)
            seen[source] = signature


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
    _drop_collection_if_other_model()
    state = _indexed_digests()
    logger.info("%d files already indexed in collection %s", len(state), QDRANT_COLLECTION)
    seen: dict[str, tuple[int, int]] = {}
    backoff = RetryBackoff()
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
            ingest_pending(pending, state, seen, backoff)
            time.sleep(SCAN_INTERVAL_SEC)
    finally:
        stop_event.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)


if __name__ == "__main__":
    main()
