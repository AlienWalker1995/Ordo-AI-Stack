"""rag-ingestion's EmbeddingGemma 2 wiring: the document prompt on every embedded chunk, the model
recorded on every point, and the automatic re-embed when the collection holds another model's
vectors. Also holds the query and document prompts of the three embedding clients together."""
from __future__ import annotations

from pathlib import Path

import ingest
import yaml

ROOT = Path(__file__).resolve().parents[2]


class _Response:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeQdrant:
    """Stands in for httpx.Client: a scroll answer of the caller's choosing, every call recorded."""

    def __init__(self, scroll: _Response):
        self.scroll = scroll
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/points/scroll"):
            return self.scroll
        if url.endswith("/v1/embeddings"):
            return _Response(200, {"data": [{"embedding": [0.1, 0.2]} for _ in json["input"]]})
        return _Response()                                   # points/delete

    def delete(self, url):
        self.calls.append(("DELETE", url, None))
        return _Response()

    def get(self, url):
        self.calls.append(("GET", url, None))
        return _Response(200)

    def put(self, url, json=None):
        self.calls.append(("PUT", url, json))
        return _Response()


def _scroll(points: list[dict]) -> _Response:
    return _Response(200, {"result": {"points": points}})


def test_each_chunk_is_embedded_with_the_document_prompt_and_stored_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    monkeypatch.setattr(ingest, "EMBED_DOCUMENT_PREFIX", "title: none | text: ")
    fake = _FakeQdrant(_scroll([]))
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    note = tmp_path / "notes" / "Wine Log.md"
    note.parent.mkdir()
    note.write_text("Aptos Vineyard pinot noir", encoding="utf-8")

    assert ingest.ingest_path(note, {}) is True

    embedded = next(body["input"] for method, url, body in fake.calls if url.endswith("/v1/embeddings"))
    assert embedded == ["title: none | text: Aptos Vineyard pinot noir"]
    points = next(body["points"] for method, url, body in fake.calls if method == "PUT" and url.endswith("/points"))
    assert points[0]["payload"]["content"] == "Aptos Vineyard pinot noir"
    assert points[0]["payload"]["embed_model"] == ingest.EMBED_MODEL


def test_a_collection_from_another_model_is_dropped(monkeypatch):
    fake = _FakeQdrant(_scroll([{"payload": {"embed_model": "nomic-embed-text-v1.5.Q4_K_M.gguf"}}]))
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    assert ingest._drop_collection_if_other_model() is True
    assert ("DELETE", f"{ingest.QDRANT_URL}/collections/{ingest.QDRANT_COLLECTION}", None) in fake.calls


def test_points_without_a_recorded_model_count_as_another_model(monkeypatch):
    """Everything indexed before this change carries no embed_model: the swap re-embeds it."""
    fake = _FakeQdrant(_scroll([{"payload": {}}]))
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    assert ingest._drop_collection_if_other_model() is True


def test_a_collection_from_the_current_model_is_kept(monkeypatch):
    fake = _FakeQdrant(_scroll([{"payload": {"embed_model": ingest.EMBED_MODEL}}]))
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    assert ingest._drop_collection_if_other_model() is False
    assert not [call for call in fake.calls if call[0] == "DELETE"]


def test_a_missing_or_empty_collection_is_left_alone(monkeypatch):
    for scroll in (_Response(404), _scroll([])):
        fake = _FakeQdrant(scroll)
        monkeypatch.setattr(ingest.httpx, "Client", fake)
        assert ingest._drop_collection_if_other_model() is False
        assert not [call for call in fake.calls if call[0] == "DELETE"]


def _env(manifest: str, service: str | None = None) -> dict:
    doc = yaml.safe_load((ROOT / "services" / manifest / "plugin.yaml").read_text(encoding="utf-8"))
    if service is None:
        return doc["mcp"]["env"]
    return next(s for s in doc["services"] if s["name"] == service)["env"]


def test_every_embedding_client_sends_the_same_prompts():
    """Ingest, the qdrant_search tool and Open WebUI share one vector space only if they share the
    model's prompts: one query prompt and one document prompt."""
    query = _env("qdrant-rag")["EMBED_QUERY_PREFIX"]
    document = _env("rag", "rag-ingestion")["EMBED_DOCUMENT_PREFIX"]
    webui = _env("open-webui", "open-webui")
    assert query == webui["RAG_EMBEDDING_QUERY_PREFIX"] == "task: search result | query: "
    assert document == webui["RAG_EMBEDDING_CONTENT_PREFIX"] == "title: none | text: "
