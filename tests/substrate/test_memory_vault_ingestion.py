"""Memory-vault semantic indexing: the Obsidian vault as a rag-ingestion input root.

Covers Phase 2 of the memory system:
  - the rag plugin mounts the vault READ-ONLY at /watch/memory-vault (nested inside the watch
    tree, so the recursive watcher/scanner needs zero extra ingester config), and
  - the ingester's hidden-path rule keeps vault internals (.obsidian/ workspace JSON, .trash/
    soft-deleted notes) out of the Qdrant index.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}

VAULT_MOUNT = "${MEMORY_VAULT_PATH:-${DATA_PATH:?DATA_PATH must be set (non-empty)}/memory-vault}:/watch/memory-vault:ro"
INGEST_PY = ROOT / "services" / "rag" / "ingest.py"


def _src(plugins, hardware=P_5090):
    return Source.from_dict(
        {"hardware": hardware, "tier": "auto", "model": "auto", "plugins": plugins}
    )


# ── render: the vault is a second, read-only input root ─────────────────────
def test_rag_ingestion_mounts_vault_read_only():
    c = render(_src(["rag"]), CATALOG, REGISTRY).compose_dict()
    ri = c["services"]["rag-ingestion"]
    assert VAULT_MOUNT in ri["volumes"]
    # nested INSIDE the primary watch mount, which must stay first and unchanged
    assert ri["volumes"][0] == "${DATA_PATH:?DATA_PATH must be set (non-empty)}/rag-input:/watch"
    # the vault mount is read-only — only the memory-vault MCP writes the vault
    vault = next(v for v in ri["volumes"] if "/watch/memory-vault" in v)
    assert vault.endswith(":ro")


def test_rag_ingestion_contract_otherwise_unchanged():
    c = render(_src(["rag"]), CATALOG, REGISTRY).compose_dict()
    ri = c["services"]["rag-ingestion"]
    # single watch root: the recursive scanner sees the nested vault without extra env
    assert ri["environment"]["WATCH_DIR"] == "/watch"
    assert ri["environment"]["QDRANT_COLLECTION"] == "${RAG_COLLECTION:-documents}"
    assert ri["environment"]["MODEL_GATEWAY_URL"] == "http://inference-engine-embed:8080"


def test_embed_server_batch_fits_a_full_chunk():
    # Embeddings require each input to fit in ONE physical batch; the upstream default
    # (n_ubatch=512) 500s on chunks over 512 tokens — a 400-word markdown chunk routinely is
    # (CONVENTIONS.md = 556 tokens, caught live). Both batch flags must be raised together.
    c = render(_src(["rag"]), CATALOG, REGISTRY).compose_dict()
    cmd = c["services"]["inference-engine-embed"]["command"]
    assert cmd[cmd.index("--batch-size") + 1] == "2048"
    assert cmd[cmd.index("--ubatch-size") + 1] == "2048"
    assert "--embeddings" in cmd


def test_rag_renders_on_cpu_with_vault_mount():
    c = render(_src(["rag"], hardware=P_CPU), CATALOG, REGISTRY).compose_dict()
    assert VAULT_MOUNT in c["services"]["rag-ingestion"]["volumes"]


# ── ingester: hidden-path rule keeps vault internals out of the index ───────
@pytest.fixture()
def ingest(monkeypatch, tmp_path):
    """Load services/rag/ingest.py by path with a stubbed httpx (not a dev dep —
    these tests never touch the network) and WATCH_DIR pointed at tmp_path."""
    if not INGEST_PY.is_file():
        pytest.skip("rag-ingestion build context not present")
    monkeypatch.setitem(sys.modules, "httpx", types.ModuleType("httpx"))
    spec = importlib.util.spec_from_file_location("rag_ingest_under_test", INGEST_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WATCH_DIR", tmp_path.resolve())
    return mod


def test_hidden_components_are_ignored(ingest, tmp_path):
    for rel in (".obsidian/app.json", ".trash/deleted-note.md", "memories/user/.gitkeep.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        assert ingest._is_hidden(p), f"{rel} must be hidden"
    visible = tmp_path / "memories" / "user" / "a-real-note.md"
    visible.write_text("x", encoding="utf-8")
    assert not ingest._is_hidden(visible)


def test_scan_skips_vault_internals(ingest, tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "old.md").write_text("gone", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "note.md").write_text("fact", encoding="utf-8")
    found = ingest._iter_supported_files()
    assert [p.name for p in found] == ["note.md"]


def test_scan_does_not_walk_into_hidden_directories(ingest, tmp_path, monkeypatch):
    # A vault's .git holds thousands of files nothing will index, each a 9p round trip.
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "note.md").write_text("internal", encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "note.md").write_text("fact", encoding="utf-8")
    (tmp_path / "notes" / ".hidden.md").write_text("internal", encoding="utf-8")
    (tmp_path / "notes" / "diagram.png").write_bytes(b"png")
    visited = []
    real_walk = ingest.os.walk

    def recording_walk(top):
        for root, dirnames, filenames in real_walk(top):
            visited.append(root)
            yield root, dirnames, filenames

    monkeypatch.setattr(ingest.os, "walk", recording_walk)
    assert ingest._iter_supported_files() == [tmp_path.resolve() / "notes" / "note.md"]
    assert not [root for root in visited if ".git" in root]


def test_ingest_path_refuses_hidden_files(ingest, tmp_path):
    hidden = tmp_path / ".trash" / "old.md"
    hidden.parent.mkdir()
    hidden.write_text("gone", encoding="utf-8")
    # returns False before any hashing/embedding/network call
    assert ingest.ingest_path(hidden, {}) is False


def test_ingest_path_still_refuses_unsupported_extensions(ingest, tmp_path):
    attachment = tmp_path / "memories" / "diagram.png"
    attachment.parent.mkdir()
    attachment.write_bytes(b"\x89PNG")
    assert ingest.ingest_path(attachment, {}) is False


# ── embed input hygiene: image links and oversized chunks ───────────────────
def test_image_targets_stripped_but_alt_text_kept(ingest):
    # Hash filenames are retrieval noise and tokenize ~10x denser than prose (a
    # 400-word run of image links reached 13k tokens live); OCR alt text is signal.
    text = (
        "intro line\n"
        "![Four Steps To Analyze](8661c936213418aea1a62a145ae992bd.png)\n"
        "![](0046c9ea1f484a54a165db88617642ba.jpeg)\n"
        "closing ![inline alt](abc123.png) text\n"
    )
    out = ingest._strip_image_targets(text)
    assert "8661c936" not in out and "0046c9ea" not in out and "abc123" not in out
    assert "Four Steps To Analyze" in out
    assert "closing inline alt text" in out
    # non-image constructs are untouched
    assert ingest._strip_image_targets("a [link](x.md) b") == "a [link](x.md) b"


class _FakeResp:
    def __init__(self, status_code, vectors=None):
        self.status_code = status_code
        self._vectors = vectors or []

    def json(self):
        return {"data": [{"embedding": v} for v in self._vectors]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _FakeClient:
    """Rejects inputs over a token budget the way inference-engine-embed does (whole batch)."""

    def __init__(self, max_chars):
        self.max_chars = max_chars
        self.calls = []

    def post(self, url, json=None, headers=None):
        texts = json["input"]
        self.calls.append(texts)
        if any(len(t) > self.max_chars for t in texts):
            return _FakeResp(500)
        return _FakeResp(200, vectors=[[0.1] * 3 for _ in texts])


def test_embed_falls_back_to_per_chunk_truncation(ingest, monkeypatch):
    # One oversized chunk must not fail the whole file: the batch 500s, then each
    # chunk embeds individually, the big one truncated by halving until it fits.
    client = _FakeClient(max_chars=100)
    monkeypatch.setattr(
        ingest.httpx, "Client",
        lambda timeout: type("Ctx", (), {
            "__enter__": lambda s: client, "__exit__": lambda s, *a: False})(),
        raising=False,
    )
    vectors = ingest._embed(["small chunk", "x" * 350])
    assert len(vectors) == 2 and all(len(v) == 3 for v in vectors)
    # halving: 350 -> 175 -> 87 (fits under 100)
    assert any(texts == ["x" * 87] for texts in client.calls)


# ── the retry loop: a large file must finish, and a failing one must not spin ───────────
class _CountingEmbedClient:
    """Answers every input, and records the batch sizes it was sent. Vectors carry the input's text
    length so a test can see they stayed in order."""

    def __init__(self, drop_last=False):
        self.batch_sizes = []
        self.drop_last = drop_last

    def post(self, url, json=None, headers=None):
        texts = json["input"]
        self.batch_sizes.append(len(texts))
        vectors = [[float(len(t))] for t in texts]
        return _FakeResp(200, vectors=vectors[:-1] if self.drop_last else vectors)


def _use_client(ingest, monkeypatch, client):
    context = type("Ctx", (), {"__enter__": lambda s: client, "__exit__": lambda s, *a: False})()
    monkeypatch.setattr(ingest.httpx, "Client", lambda timeout: context, raising=False)


def test_a_large_file_is_embedded_in_bounded_requests(ingest, monkeypatch):
    # One request per file timed out on every file over about 400 chunks (3.4 chunks/s against a
    # 120 s timeout), and the engine kept computing the abandoned request.
    client = _CountingEmbedClient()
    _use_client(ingest, monkeypatch, client)
    monkeypatch.setattr(ingest, "EMBED_BATCH_SIZE", 3)
    vectors = ingest._embed(["a" * n for n in range(1, 8)])
    assert client.batch_sizes == [3, 3, 1]
    assert vectors == [[float(n)] for n in range(1, 8)]


def test_a_short_embed_answer_fails_instead_of_misaligning_chunks(ingest, monkeypatch):
    _use_client(ingest, monkeypatch, _CountingEmbedClient(drop_last=True))
    with pytest.raises(RuntimeError, match="vectors"):
        ingest._embed(["one", "two"])


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _run_pass(ingest, paths, seen, backoff, attempts, fail):
    def fake_ingest(path, state):
        attempts.append(path.name)
        if path.name in fail:
            raise RuntimeError("timed out")
        return True

    original = ingest.ingest_path
    ingest.ingest_path = fake_ingest
    try:
        ingest.ingest_pending(paths, {}, seen, backoff)
    finally:
        ingest.ingest_path = original


def test_a_failing_file_backs_off_and_the_others_still_run(ingest, tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("bad", encoding="utf-8")
    clock = _FakeClock()
    backoff = ingest.RetryBackoff(clock=clock)
    seen, attempts = {}, []

    def attempts_after(seconds):
        attempts.clear()
        clock.now += seconds
        _run_pass(ingest, [bad], seen, backoff, attempts, {"bad.md"})
        return len(attempts)

    assert attempts_after(0) == 1
    assert attempts_after(15) == 0
    assert attempts_after(45) == 1      # 60 s after the first failure
    assert attempts_after(60) == 0      # the wait doubled to 120 s
    assert attempts_after(60) == 1
    for _ in range(12):                 # however long it keeps failing, it never waits over the cap
        clock.now += ingest.RETRY_MAX_SECONDS
        assert attempts_after(0) == 1

    good = tmp_path / "good.md"
    good.write_text("good", encoding="utf-8")
    attempts.clear()
    _run_pass(ingest, [bad, good], seen, backoff, attempts, {"bad.md"})
    assert attempts == ["good.md"]


def test_editing_a_failing_file_retries_it_at_once(ingest, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("first", encoding="utf-8")
    backoff = ingest.RetryBackoff(clock=_FakeClock())
    seen, attempts = {}, []
    _run_pass(ingest, [note], seen, backoff, attempts, {"note.md"})
    note.write_text("second, longer", encoding="utf-8")
    _run_pass(ingest, [note], seen, backoff, attempts, set())
    assert attempts == ["note.md", "note.md"]
    # and a success clears the record
    _run_pass(ingest, [note], seen, backoff, attempts, set())
    assert attempts == ["note.md", "note.md"]


def test_an_unchanged_file_is_not_read_again(ingest, tmp_path):
    # Hashing every file on every scan kept the 9p mount busy around the clock.
    note = tmp_path / "note.md"
    note.write_text("text", encoding="utf-8")
    backoff = ingest.RetryBackoff(clock=_FakeClock())
    seen, attempts = {}, []
    _run_pass(ingest, [note], seen, backoff, attempts, set())
    _run_pass(ingest, [note], seen, backoff, attempts, set())
    assert attempts == ["note.md"]
    note.write_text("changed text", encoding="utf-8")
    _run_pass(ingest, [note], seen, backoff, attempts, set())
    assert attempts == ["note.md", "note.md"]


class _ScrollClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def post(self, url, json=None):
        self.requests.append(json)
        status, points, next_offset = self.pages.pop(0)
        return types.SimpleNamespace(
            status_code=status,
            raise_for_status=lambda: None,
            json=lambda: {"result": {"points": points, "next_page_offset": next_offset}},
        )


def test_what_is_indexed_comes_from_the_store_across_pages(ingest, monkeypatch):
    # The record lived in the container's /tmp, so every recreate re-embedded the whole index.
    client = _ScrollClient([
        (200, [{"payload": {"source": "a.md", "digest": "1"}}, {"payload": {"source": "a.md", "digest": "1"}}], "next"),
        (200, [{"payload": {"source": "b.md", "digest": "2"}}, {"payload": {"source": "old.md"}}], None),
    ])
    _use_client(ingest, monkeypatch, client)
    assert ingest._indexed_digests() == {"a.md": "1", "b.md": "2"}
    assert client.requests[1]["offset"] == "next"
    assert all(request["with_vector"] is False for request in client.requests)


def test_a_missing_collection_means_nothing_is_indexed(ingest, monkeypatch):
    _use_client(ingest, monkeypatch, _ScrollClient([(404, [], None)]))
    assert ingest._indexed_digests() == {}


def test_the_batch_size_and_scan_interval_are_configured_by_the_plugin():
    plugin = (ROOT / "services" / "rag" / "plugin.yaml").read_text(encoding="utf-8")
    assert "EMBED_BATCH_SIZE: ${RAG_EMBED_BATCH_SIZE:-16}" in plugin
    # A scan costs about 30 s of 9p round trips, so a shorter wait would keep the mount busy.
    assert "SCAN_INTERVAL_SEC: ${RAG_SCAN_INTERVAL_SEC:-120}" in plugin


def test_a_file_key_costs_no_filesystem_access(ingest, monkeypatch):
    # resolve() stats every path component over the 9p mount, for every file, on every scan.
    def no_resolve(self, strict=False):
        raise AssertionError("resolve() must not be called")

    watch_dir = ingest.WATCH_DIR
    monkeypatch.setattr(ingest.Path, "resolve", no_resolve)
    assert ingest._file_key(watch_dir / "notes" / "a.md") == "notes/a.md"
