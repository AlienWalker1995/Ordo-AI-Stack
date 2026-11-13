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
from datetime import date, datetime, timedelta
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
SCAN_INTERVAL_SEC = max(5, int(os.environ.get("SCAN_INTERVAL_SEC", "120")))
# How often the index is reconciled with the disk (indexed sources whose file is gone, or that the
# rules no longer ingest, are deleted). Docker Desktop bind mounts may emit no delete events, so
# this pass is what removes orphans there. The first one runs right after the first full pass.
RECONCILE_INTERVAL_SEC = max(60, int(os.environ.get("RECONCILE_INTERVAL_SEC", "600")))
# A pass that would delete more than this fraction of the indexed sources is refused: it looks like
# a mount that lost files, not an operator deleting half the notes.
RECONCILE_MAX_DELETE_FRACTION = float(os.environ.get("RECONCILE_MAX_DELETE_FRACTION", "0.5"))
RECONCILE_ALLOW_MASS_DELETE = os.environ.get("RECONCILE_ALLOW_MASS_DELETE", "").strip().lower() in {"1", "true", "yes"}
# Chunks per embedding request. The engine computes about 3.4 chunks a second and keeps computing a
# request after its client gives up, so one request must stay far inside EMBED_TIMEOUT_SECONDS.
EMBED_BATCH_SIZE = max(1, int(os.environ.get("EMBED_BATCH_SIZE", "16")))
EMBED_TIMEOUT_SECONDS = 120.0
# A file that chunks into more than this is a data dump (a JSON catalog, a generated report), not a
# document: skipped before it is embedded. Measured 2026-11-07: every document in the watch tree is
# under 40 chunks, the dumps 225 to 3,350, and embedding 3,350 chunks held every core for ~17 minutes.
MAX_CHUNKS_PER_FILE = max(1, int(os.environ.get("MAX_CHUNKS_PER_FILE", "200")))
# Points per Qdrant upsert request. Qdrant refuses a request body over 32 MB, and one point (768
# floats as JSON plus its chunk text) is about 15 KB, so 256 points keep a request near 4 MB.
UPSERT_BATCH_POINTS = 256
# How long a file that failed waits before its next attempt: doubles from the first to the cap.
RETRY_FIRST_SECONDS = 60.0
RETRY_MAX_SECONDS = 3600.0
HEARTBEAT_PATH = Path("/tmp/rag-ingestion.heartbeat")

# Authority: which source wins when two documents disagree (policy: the operator's
# agent-memory-and-context-poisoning doc). Decided by WHERE a file lives, never by what it claims:
# a note that writes `authority: canonical` in its own frontmatter is still derived.
#   canonical  the operator's source-of-truth folder(s), named by CANONICAL_PREFIXES: paths under
#              the watch tree, comma-separated. By default the vault's notes/canonical/. The folder
#              is the operator's alone: agent-facing containers should mount it read-only.
#   ephemeral  the memory vault's inbox/ (unreviewed captures)
#   derived    everything else (notes, memories, runbooks, other docs)
DEFAULT_CANONICAL_PREFIXES = "memory-vault/notes/canonical/"


def _folder_prefixes(raw: str) -> tuple[str, ...]:
    """Comma-separated folders as source prefixes, each ending in one slash so `canonical/` never
    matches `canonical-drafts/`. Blank entries are dropped."""
    folders = (entry.strip().strip("/") for entry in raw.split(","))
    return tuple(f"{folder}/" for folder in folders if folder)


CANONICAL_PREFIXES = _folder_prefixes(os.environ.get("CANONICAL_PREFIXES", DEFAULT_CANONICAL_PREFIXES))
# Never indexed: the vault's copies of agent homes (agents/hermes/: thousands of skill files,
# scripts and configs, refreshed daily by the operator's vault backup). The agent reads its own
# skills directly; indexing the copy would bury notes under machinery and spend hours embedding it.
EXCLUDED_PREFIXES = ("memory-vault/agents/",)
EPHEMERAL_PREFIXES = ("memory-vault/inbox/",)
# History by location: every document under these folders is stored as `archived`, whatever its
# frontmatter says, so old plans, logs and archives stay searchable on request (include_history)
# without one line of frontmatter per file. Comma-separated, like CANONICAL_PREFIXES.
HISTORY_PREFIXES = _folder_prefixes(os.environ.get("HISTORY_PREFIXES", "memory-vault/archive/"))
# How many ancestor folders of a document are stored for folder-scoped search.
FOLDER_DEPTH = 4
# Bumped when the metadata rules change: points stored under an older version get their payload
# rewritten in place at the next scan (no re-embedding). 2: status is normalised and last_verified
# validated, so a stored "archived # old" or a future date is corrected. 3: history folders, and
# each chunk's ancestor folders for folder-scoped search.
META_VERSION = 3
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
INLINE_COMMENT_RE = re.compile(r"(^|\s)#.*$")
# Every status a document can have in the index. Synonyms map onto these; anything else is current.
STATUS_SYNONYMS = {"deprecated": "archived", "obsolete": "archived"}
KNOWN_STATUSES = {"current", "superseded", "archived"}

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


def _indexed_digests() -> tuple[dict[str, str], dict[str, str]]:
    """({source: digest} of every file indexed with current metadata, {source: digest} of the files
    whose stored metadata predates META_VERSION). The store is the record of what is indexed, so a
    recreated container re-embeds nothing and an emptied store re-embeds everything."""
    digests: dict[str, str] = {}
    stale_meta: dict[str, str] = {}
    request = {"limit": 1000, "with_payload": ["source", "digest", "meta_version"], "with_vector": False}
    with httpx.Client(timeout=30.0) as client:
        while True:
            r = client.post(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/scroll", json=request)
            if r.status_code == 404:
                return {}, {}
            r.raise_for_status()
            result = r.json().get("result", {})
            for point in result.get("points", []):
                payload = point.get("payload") or {}
                if payload.get("source") and payload.get("digest"):
                    current = payload.get("meta_version") == META_VERSION
                    (digests if current else stale_meta)[payload["source"]] = payload["digest"]
            next_offset = result.get("next_page_offset")
            if next_offset is None:
                for source in digests.keys() & stale_meta.keys():  # one chunk lagging = the file lags
                    del digests[source]
                return digests, stale_meta
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


def _source_condition(source: str) -> dict:
    return {"key": "source", "match": {"value": source}}


def _delete_points(client: httpx.Client, point_filter: dict) -> None:
    r = client.post(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/delete",
        params={"wait": "true"},
        json={"filter": point_filter},
    )
    r.raise_for_status()


def _delete_source(source: str) -> None:
    with httpx.Client(timeout=60.0) as client:
        _delete_points(client, {"must": [_source_condition(source)]})


def _scalar(value: str) -> str:
    """A frontmatter value without its quotes or its trailing YAML comment (` # ...`)."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing > 0:
            return value[1:closing]
    return INLINE_COMMENT_RE.sub("", value).strip().strip("'\"")


def _frontmatter(text: str) -> dict[str, str]:
    """The flat `key: value` lines of a leading YAML frontmatter block, lowercased keys, quotes and
    inline comments stripped. Nested values and lists are ignored: only scalar fields are read."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = _scalar(value)
        if value:
            fields[key.strip().lower()] = value
    return fields


def _normalise_status(raw: str | None) -> str:
    """current, superseded or archived: the only values the search filter understands."""
    status = (raw or "").strip().lower()
    status = STATUS_SYNONYMS.get(status, status)
    return status if status in KNOWN_STATUSES else "current"


def _valid_past_date(raw: str | None) -> str | None:
    """`raw` when it is a YYYY-MM-DD date that is not in the future, otherwise None. Tomorrow by
    this container's clock still passes: it is already today's date east of the container's zone."""
    if not raw or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None
    return raw if parsed <= date.today() + timedelta(days=1) else None


def _ancestor_folders(source: str) -> list[str]:
    """The folders `source` sits in, outermost first, at most FOLDER_DEPTH of them:
    `vault/notes/work/a.md` gives `vault`, `vault/notes`, `vault/notes/work`. A search scoped to
    any of them finds the document."""
    parts = source.split("/")[:-1][:FOLDER_DEPTH]
    return ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def _document_meta(source: str, text: str) -> dict:
    """The authority, status, last-verified date and folders stored with every chunk of `source`."""
    if source.startswith(CANONICAL_PREFIXES):
        authority = "canonical"
    elif source.startswith(EPHEMERAL_PREFIXES):
        authority = "ephemeral"
    else:
        authority = "derived"
    fields = _frontmatter(text) if Path(source).suffix.lower() in {".md", ".markdown"} else {}
    last_verified = _valid_past_date(fields.get("last_verified")) or _valid_past_date(fields.get("modified"))
    status = _normalise_status(fields.get("status"))
    if status == "current" and source.startswith(HISTORY_PREFIXES):
        status = "archived"   # a history folder: where the file lives decides, not what it claims
    return {
        "authority": authority,
        "status": status,
        "last_verified": last_verified,
        "folders": _ancestor_folders(source),
        "meta_version": META_VERSION,
    }


def _set_meta(source: str, meta: dict) -> None:
    """Rewrite the metadata of every stored chunk of `source` in place."""
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/payload",
            params={"wait": "true"},
            json={"payload": meta, "filter": {"must": [_source_condition(source)]}},
        )
        r.raise_for_status()


def _upsert_points(source: str, digest: str, chunks: list[str], vectors: list[list[float]],
                   meta: dict | None = None) -> None:
    """Writes the new version of `source` in requests small enough for Qdrant, then removes every
    other point of that source. A failed write removes what it wrote: its points carry the new digest,
    so a half-written version would read as fully indexed after a restart."""
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
                    **(meta or {}),
                },
            }
        )
    with httpx.Client(timeout=60.0) as client:
        try:
            for start in range(0, len(points), UPSERT_BATCH_POINTS):
                r = client.put(
                    f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
                    params={"wait": "true"},
                    json={"points": points[start : start + UPSERT_BATCH_POINTS]},
                )
                r.raise_for_status()
        except Exception:
            _delete_points(client, {"must": [
                _source_condition(source),
                {"key": "digest", "match": {"value": digest}},
            ]})
            raise
        # Only now drop the previous version, so a failed write leaves the old one searchable.
        _delete_points(client, {
            "must": [_source_condition(source)],
            "must_not": [{"has_id": [point["id"] for point in points]}],
        })


def _is_excluded(source: str) -> bool:
    return source.startswith(EXCLUDED_PREFIXES)


def _iter_supported_files() -> list[Path]:
    """Every file to index. Hidden directories are not entered at all, and names alone decide the
    rest: each extra stat here is a round trip over the 9p mount, repeated on every scan."""
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(WATCH_DIR):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")
                       and not _is_excluded(_file_key(Path(root) / name) + "/")]
        for name in filenames:
            path = Path(root) / name
            if not name.startswith(".") and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(path)
    return sorted(found)


def _is_ingestable_name(path: Path) -> bool:
    """True when the rules allow `path` into the index, judged by its name alone."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS and not _is_hidden(path) and not _is_excluded(_file_key(path))


def ingest_path(path: Path, state: dict[str, str], stale_meta: dict[str, str] | None = None) -> bool:
    """Index `path` if its content changed. A file whose content is already indexed but whose stored
    metadata is out of date (listed in `stale_meta`) gets its metadata rewritten, not re-embedded."""
    if not path.is_file() or not _is_ingestable_name(path):  # watch events still arrive for excluded folders
        return False
    source = _file_key(path)
    digest = _sha256(path)
    if state.get(source) == digest:
        return False
    if stale_meta is not None and stale_meta.get(source) == digest:
        _set_meta(source, _document_meta(source, _read_text(path)))
        del stale_meta[source]
        state[source] = digest
        logger.info("Updated metadata of %s", source)
        return False
    text = _strip_image_targets(_read_text(path))
    chunks = _chunk(text, CHUNK_SIZE, CHUNK_OVERLAP)
    if not chunks:
        logger.info("Skipping %s: no extractable text", source)
        state[source] = digest
        return False
    if len(chunks) > MAX_CHUNKS_PER_FILE:
        logger.info("Skipping %s: %d chunks is over MAX_CHUNKS_PER_FILE=%d (a data dump, not a document)",
                    source, len(chunks), MAX_CHUNKS_PER_FILE)
        if source in state:
            _delete_source(source)
        state[source] = digest
        return False
    vectors = _embed([EMBED_DOCUMENT_PREFIX + chunk for chunk in chunks])
    _upsert_points(source, digest, chunks, vectors, _document_meta(source, text))
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
                   backoff: RetryBackoff, stale_meta: dict[str, str] | None = None) -> None:
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
            ingest_path(path, state, stale_meta)
        except Exception as exc:
            wait = backoff.record_failure(source, signature)
            logger.exception("Failed to ingest %s: %s (next attempt in %.0f s)", path, exc, wait)
        else:
            backoff.record_success(source)
            seen[source] = signature


def _source_root(source: str) -> str:
    """The top folder of a source (a mount under the watch tree), or "." for a top-level file."""
    return source.split("/", 1)[0] if "/" in source else "."


def _drop_unverifiable_roots(orphans: list[str], wanted: set[str]) -> list[str]:
    """The orphans under a root that still has files on disk. A root with none is a mount that is
    not ready or was evicted: nothing in it can be called deleted."""
    present_roots = {_source_root(source) for source in wanted}
    kept = [source for source in orphans if _source_root(source) in present_roots]
    for root in sorted({_source_root(source) for source in orphans} - present_roots):
        logger.warning("Reconcile skipped root %s: no files on disk (mount not ready?), its indexed "
                       "sources are kept", root)
    return kept


def _forget(source: str, state: dict[str, str], stale_meta: dict[str, str],
            seen: dict[str, tuple[int, int]]) -> None:
    state.pop(source, None)
    stale_meta.pop(source, None)
    seen.pop(source, None)


def reconcile_index(state: dict[str, str], stale_meta: dict[str, str],
                    seen: dict[str, tuple[int, int]]) -> int:
    """Deletes every indexed source whose file is gone or that the rules no longer ingest (excluded
    prefix, hidden folder, unsupported type). Returns how many sources were deleted. A root with no
    files on disk is skipped, and a pass that would delete too large a share of the index is refused."""
    wanted = {_file_key(path) for path in _iter_supported_files()}
    indexed = state.keys() | stale_meta.keys()
    orphans = _drop_unverifiable_roots(sorted(indexed - wanted), wanted)
    if not orphans:
        return 0
    if len(orphans) > RECONCILE_MAX_DELETE_FRACTION * len(indexed) and not RECONCILE_ALLOW_MASS_DELETE:
        logger.error("Reconcile REFUSED: it would delete %d of %d indexed sources (over %.0f%%). Set "
                     "RECONCILE_ALLOW_MASS_DELETE=1 if the files really are gone.",
                     len(orphans), len(indexed), RECONCILE_MAX_DELETE_FRACTION * 100)
        return 0
    deleted = 0
    for source in orphans:
        try:
            _delete_source(source)
        except Exception as exc:
            logger.error("Reconcile could not delete %s: %s (retried next pass)", source, exc)
            continue
        _forget(source, state, stale_meta, seen)
        deleted += 1
    logger.info("Reconcile deleted %d of %d indexed sources", deleted, len(indexed))
    return deleted


def an_indexed_file_is_gone(paths: list[Path], state: dict[str, str], stale_meta: dict[str, str]) -> bool:
    """True when a delete or move event names an indexed file that is no longer on disk. A path that
    is a file again (an editor's save-by-replace) does not count.

    The event deletes nothing itself. It only makes the next reconcile run now instead of at its
    interval, so every deletion goes through the reconcile's guards: a mount that drops out sends a
    delete event for every file in it, and must not wipe them from the index."""
    for path in paths:
        if path.is_file() and _is_ingestable_name(path):
            continue
        source = _file_key(path)
        if source in state or source in stale_meta:
            return True
    return False


def _heartbeat(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")
        stop_event.wait(10)


class _EventHandler(FileSystemEventHandler):
    def __init__(self, queue: set[Path], lock: threading.Lock, removed: set[Path] | None = None) -> None:
        self._queue = queue
        self._lock = lock
        self._removed = removed if removed is not None else set()

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

    def on_deleted(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return  # a deleted folder's files are removed by the next reconcile
        with self._lock:
            self._removed.add(Path(event.src_path))

    def on_moved(self, event):  # type: ignore[override]
        if getattr(event, "is_directory", False):
            return  # a moved folder is picked up by the next rescan and reconcile
        with self._lock:
            self._removed.add(Path(event.src_path))
            self._queue.add(Path(event.dest_path))


def main() -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    _drop_collection_if_other_model()
    state, stale_meta = _indexed_digests()
    logger.info("%d files already indexed in collection %s (%d need a metadata update)",
                len(state) + len(stale_meta), QDRANT_COLLECTION, len(stale_meta))
    seen: dict[str, tuple[int, int]] = {}
    backoff = RetryBackoff()
    stop_event = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(stop_event,), daemon=True)
    heartbeat.start()

    queue: set[Path] = set(_iter_supported_files())
    removed: set[Path] = set()
    queue_lock = threading.Lock()
    last_reconcile: float | None = None
    observer = None
    if Observer is not None:
        observer = Observer()
        observer.schedule(_EventHandler(queue, queue_lock, removed), str(WATCH_DIR), recursive=True)
        observer.start()

    logger.info("Watching %s for RAG ingestion into collection %s", WATCH_DIR, QDRANT_COLLECTION)
    try:
        while True:
            with queue_lock:
                if not queue:
                    queue.update(_iter_supported_files())
                pending = list(queue)
                queue.clear()
                gone = list(removed)
                removed.clear()
            reconcile_now = an_indexed_file_is_gone(gone, state, stale_meta)
            ingest_pending(pending, state, seen, backoff, stale_meta)
            if (reconcile_now or last_reconcile is None
                    or time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_SEC):
                last_reconcile = time.monotonic()
                try:
                    reconcile_index(state, stale_meta, seen)
                except Exception:
                    logger.exception("Reconcile failed; it runs again next interval")
            time.sleep(SCAN_INTERVAL_SEC)
    finally:
        stop_event.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)


if __name__ == "__main__":
    main()
