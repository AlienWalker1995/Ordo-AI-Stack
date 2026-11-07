"""rag-ingestion on large files: an upsert is split under Qdrant's 32 MB request limit, a failed upsert
leaves no half-written version behind, and a file that chunks past MAX_CHUNKS_PER_FILE (a data dump,
not a document) is skipped before anything is embedded.

Regression for 2026-11-07: an 11 MB JSON catalog (3,350 chunks) was embedded for ~17 minutes on every
core, then refused by Qdrant in one 48 MB request, and retried every hour forever."""
from __future__ import annotations

import json

import ingest
import pytest

QDRANT_REQUEST_LIMIT_BYTES = 32 * 1024 * 1024


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeQdrant:
    """Stands in for httpx.Client against Qdrant: records every request, and answers each upsert PUT
    with the next status in `put_statuses` (200 once they run out)."""

    def __init__(self, put_statuses: list[int] | None = None):
        self.requests: list[tuple[str, str, dict]] = []
        self.put_statuses = list(put_statuses or [])

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put(self, url, json=None, params=None):
        self.requests.append(("PUT", url, json))
        return FakeResponse(self.put_statuses.pop(0) if self.put_statuses else 200)

    def post(self, url, json=None, params=None):
        self.requests.append(("POST", url, json))
        return FakeResponse(200)

    def upserts(self) -> list[dict]:
        return [body for method, url, body in self.requests if method == "PUT" and url.endswith("/points")]

    def deletes(self) -> list[dict]:
        return [body for method, url, body in self.requests if url.endswith("/points/delete")]


@pytest.fixture
def qdrant(monkeypatch):
    def install(put_statuses: list[int] | None = None) -> FakeQdrant:
        fake = FakeQdrant(put_statuses)
        monkeypatch.setattr(ingest.httpx, "Client", fake)
        monkeypatch.setattr(ingest, "_ensure_collection", lambda vector_size: None)
        return fake
    return install


def _chunks_and_vectors(count: int) -> tuple[list[str], list[list[float]]]:
    chunks = [f"chunk {i} " + "word " * 399 for i in range(count)]
    vectors = [[0.0123456789] * 768 for _ in range(count)]
    return chunks, vectors


def test_a_large_file_is_upserted_in_requests_under_qdrants_limit(qdrant):
    fake = qdrant()
    chunks, vectors = _chunks_and_vectors(3350)

    ingest._upsert_points("big.json", "digest-new", chunks, vectors)

    upserts = fake.upserts()
    assert len(upserts) > 1
    assert sum(len(body["points"]) for body in upserts) == 3350
    for body in upserts:
        assert len(json.dumps(body).encode()) < QDRANT_REQUEST_LIMIT_BYTES


def test_the_previous_version_is_removed_only_after_the_new_one_is_written(qdrant):
    fake = qdrant()
    chunks, vectors = _chunks_and_vectors(600)

    ingest._upsert_points("big.json", "digest-new", chunks, vectors)

    method, url, body = fake.requests[-1]
    assert url.endswith("/points/delete")
    written_ids = {point["id"] for upsert in fake.upserts() for point in upsert["points"]}
    point_filter = body["filter"]
    assert point_filter["must"] == [{"key": "source", "match": {"value": "big.json"}}]
    assert set(point_filter["must_not"][0]["has_id"]) == written_ids


def test_a_failed_upsert_removes_its_partial_version_and_keeps_the_old_one(qdrant):
    fake = qdrant(put_statuses=[200, 400])
    chunks, vectors = _chunks_and_vectors(600)

    with pytest.raises(RuntimeError):
        ingest._upsert_points("big.json", "digest-new", chunks, vectors)

    deletes = fake.deletes()
    assert len(deletes) == 1
    assert deletes[0]["filter"] == {"must": [
        {"key": "source", "match": {"value": "big.json"}},
        {"key": "digest", "match": {"value": "digest-new"}},
    ]}


def test_a_file_over_the_chunk_cap_is_skipped_without_embedding(tmp_path, monkeypatch, qdrant):
    fake = qdrant()
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    monkeypatch.setattr(ingest, "MAX_CHUNKS_PER_FILE", 3)
    monkeypatch.setattr(ingest, "_embed", lambda chunks: pytest.fail("a data dump must not be embedded"))
    dump = tmp_path / "_catalog.json"
    dump.write_text("word " * 2000, encoding="utf-8")
    state: dict[str, str] = {}

    assert ingest.ingest_path(dump, state) is False
    assert state["_catalog.json"] == ingest._sha256(dump)
    assert fake.requests == []


def test_a_previously_indexed_file_that_grows_past_the_cap_leaves_the_index(tmp_path, monkeypatch, qdrant):
    fake = qdrant()
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    monkeypatch.setattr(ingest, "MAX_CHUNKS_PER_FILE", 3)
    monkeypatch.setattr(ingest, "_embed", lambda chunks: pytest.fail("a data dump must not be embedded"))
    dump = tmp_path / "_portal_index.json"
    dump.write_text("word " * 2000, encoding="utf-8")

    assert ingest.ingest_path(dump, {"_portal_index.json": "digest-old"}) is False
    assert fake.deletes() == [{"filter": {"must": [{"key": "source", "match": {"value": "_portal_index.json"}}]}}]


def test_a_file_at_the_chunk_cap_is_still_indexed(tmp_path, monkeypatch, qdrant):
    fake = qdrant()
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    monkeypatch.setattr(ingest, "MAX_CHUNKS_PER_FILE", 3)
    monkeypatch.setattr(ingest, "CHUNK_SIZE", 10)
    monkeypatch.setattr(ingest, "CHUNK_OVERLAP", 0)
    monkeypatch.setattr(ingest, "_embed", lambda chunks: [[0.1] * 768 for _ in chunks])
    note = tmp_path / "note.md"
    note.write_text("word " * 30, encoding="utf-8")

    assert ingest.ingest_path(note, {}) is True
    assert sum(len(body["points"]) for body in fake.upserts()) == 3
