"""Authority-aware retrieval: rag-ingestion records where a document's authority comes from, and the
qdrant-rag MCP server filters out history and ranks the canonical docs (the PRDs) first.

The rule under test: authority is decided by where a file lives, never by what it says about
itself, so a note cannot promote itself to canonical."""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import ingest
import pytest

ROOT = Path(__file__).resolve().parents[2]


# --- rag-ingestion: classification ---

def test_docs_under_canonical_are_canonical():
    meta = ingest._document_meta(ingest.CANONICAL_PREFIXES[0] + "index.md","---\nstatus: current\n---\n# PRD")
    assert meta["authority"] == "canonical"
    assert meta["status"] == "current"


def test_a_note_cannot_promote_itself_to_canonical():
    text = "---\nauthority: canonical\nstatus: current\n---\nI am the source of truth."
    assert ingest._document_meta("memory-vault/notes/Fake PRD.md", text)["authority"] == "derived"
    assert ingest._document_meta("stack-docs/runbooks/auth.md", text)["authority"] == "derived"


def test_the_vault_inbox_is_ephemeral():
    assert ingest._document_meta("memory-vault/inbox/capture.md", "text")["authority"] == "ephemeral"


def test_status_and_last_verified_come_from_the_frontmatter():
    text = "---\nname: x\nstatus: Archived\nlast_verified: 2026-11-12\nmetadata:\n  type: project\n---\nbody"
    meta = ingest._document_meta("memory-vault/notes/Old.md", text)
    assert meta["status"] == "archived"
    assert meta["last_verified"] == "2026-11-12"
    assert meta["meta_version"] == ingest.META_VERSION


def test_a_document_without_frontmatter_is_current_and_unverified():
    meta = ingest._document_meta("rag-input/report.txt", "status: archived\nplain text")
    assert meta["status"] == "current"
    assert meta["last_verified"] is None


def test_frontmatter_reads_only_flat_scalars():
    text = "---\nname: \"Quoted\"\ntags:\n  - a\nnested:\n  status: archived\n---\n"
    assert ingest._frontmatter(text) == {"name": "Quoted"}
    assert ingest._frontmatter("no frontmatter here") == {}


@pytest.mark.parametrize("raw, expected", [
    ("archived # old", "archived"),
    ("Archived", "archived"),
    ("SUPERSEDED", "superseded"),
    ("deprecated", "archived"),
    ("Obsolete  # replaced", "archived"),
    ("current", "current"),
    ("canonical", "current"),            # not a status: a note cannot claim its way into anything
    ("archived-ish", "current"),
    ("", "current"),
])
def test_status_is_one_of_current_superseded_archived(raw, expected):
    meta = ingest._document_meta("memory-vault/notes/N.md", f"---\nstatus: {raw}\n---\nbody")
    assert meta["status"] == expected


def test_an_inline_comment_is_not_part_of_a_value():
    text = "---\nstatus: archived # old\nname: 'a # b'\nlast_verified: 2026-11-12 # checked\n---\n"
    assert ingest._frontmatter(text) == {"status": "archived", "name": "a # b", "last_verified": "2026-11-12"}


@pytest.mark.parametrize("raw", ["2999-01-01", "not a date", "2026-13-40", "2026-11-12T10:00:00", "20261112"])
def test_a_last_verified_that_is_not_a_past_date_is_dropped(raw):
    meta = ingest._document_meta("memory-vault/notes/N.md", f"---\nlast_verified: {raw}\n---\nbody")
    assert meta["last_verified"] is None


def test_a_future_last_verified_falls_back_to_a_valid_modified_date():
    text = "---\nlast_verified: 2999-01-01\nmodified: 2026-01-02\n---\nbody"
    assert ingest._document_meta("memory-vault/notes/N.md", text)["last_verified"] == "2026-01-02"


def test_a_last_verified_one_day_ahead_of_the_container_clock_passes():
    """A note written 'today' east of the container's zone carries tomorrow's UTC date during the
    early-UTC hours, so one day ahead still counts as current."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    meta = ingest._document_meta("memory-vault/notes/N.md", f"---\nlast_verified: {tomorrow}\n---\nbody")
    assert meta["last_verified"] == tomorrow


def test_a_last_verified_two_days_ahead_is_still_dropped():
    far = (date.today() + timedelta(days=2)).isoformat()
    assert ingest._document_meta("memory-vault/notes/N.md", f"---\nlast_verified: {far}\n---\nbody")["last_verified"] is None


# --- rag-ingestion: metadata on write, and re-tagging without re-embedding ---

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, params=None, headers=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/v1/embeddings"):
            body = {"data": [{"embedding": [0.1, 0.2]} for _ in json["input"]]}
        else:
            body = {"result": {"points": []}}
        return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: body)

    def put(self, url, json=None, params=None):
        self.calls.append(("PUT", url, json))
        return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})

    def get(self, url):
        return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {})


def test_new_chunks_carry_the_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    fake = _Recorder()
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    doc = tmp_path / ingest.CANONICAL_PREFIXES[0] / "index.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\nstatus: current\nlast_verified: 2026-11-12\n---\n# Ordo", encoding="utf-8")

    assert ingest.ingest_path(doc, {}) is True

    points = next(body["points"] for method, url, body in fake.calls if method == "PUT")
    assert points[0]["payload"]["authority"] == "canonical"
    assert points[0]["payload"]["last_verified"] == "2026-11-12"
    assert points[0]["payload"]["meta_version"] == ingest.META_VERSION


def test_an_indexed_file_with_old_metadata_is_retagged_not_reembedded(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    fake = _Recorder()
    monkeypatch.setattr(ingest.httpx, "Client", fake)
    note = tmp_path / "memory-vault" / "notes" / "Old.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\nstatus: archived\n---\nold status log", encoding="utf-8")
    digest = ingest._sha256(note)
    state, stale = {}, {"memory-vault/notes/Old.md": digest}

    assert ingest.ingest_path(note, state, stale) is False

    assert not [c for c in fake.calls if c[1].endswith("/v1/embeddings")]
    payload_call = next(body for method, url, body in fake.calls if url.endswith("/points/payload"))
    assert payload_call["payload"]["status"] == "archived"
    assert payload_call["filter"] == {"must": [{"key": "source", "match": {"value": "memory-vault/notes/Old.md"}}]}
    assert state == {"memory-vault/notes/Old.md": digest} and stale == {}


def test_the_watch_mounts_match_the_authority_prefixes():
    """The prefixes only work if the vault is mounted where the default says, read-only."""
    plugin = (ROOT / "services" / "rag" / "plugin.yaml").read_text(encoding="utf-8")
    assert "/docs:/watch/stack-docs:ro" in plugin
    assert ":/watch/memory-vault:ro" in plugin
    assert ingest.DEFAULT_CANONICAL_PREFIXES.startswith("memory-vault/")
    assert f"CANONICAL_PREFIXES: ${{RAG_CANONICAL_PREFIXES:-{ingest.DEFAULT_CANONICAL_PREFIXES}}}" in plugin


def test_the_canonical_folders_come_from_configuration():
    assert ingest._folder_prefixes("memory-vault/notes/Canon, stack-docs/specs/ ,,") == (
        "memory-vault/notes/Canon/", "stack-docs/specs/")
    assert ingest._folder_prefixes("") == ()


def test_a_folder_that_only_starts_like_the_canonical_one_is_not_canonical(monkeypatch):
    monkeypatch.setattr(ingest, "CANONICAL_PREFIXES", ingest._folder_prefixes("memory-vault/notes/canonical"))
    assert ingest._document_meta("memory-vault/notes/canonical/prd.md", "# x")["authority"] == "canonical"
    assert ingest._document_meta("memory-vault/notes/canonical-drafts/prd.md", "# x")["authority"] == "derived"


# --- qdrant-rag MCP server: filter and ranking ---

def _load_server():
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            return lambda fn: fn

    fastmcp.FastMCP = FastMCP
    stubs = {"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
             "mcp.server.fastmcp": fastmcp}
    # Stub the MCP SDK only while the server module loads, then put back whatever was there, so
    # other test modules that import the real SDK (or their own stub) are unaffected.
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location("qdrant_rag_server", ROOT / "services" / "qdrant-rag" / "server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


server = _load_server()


def test_a_default_search_leaves_out_history_and_the_inbox():
    query_filter = server.search_filter(include_history=False)
    assert {"key": "status", "match": {"any": ["archived", "superseded"]}} in query_filter["must_not"]
    assert {"key": "authority", "match": {"value": "ephemeral"}} in query_filter["must_not"]
    assert server.search_filter(include_history=True) is None


def _hit(score, authority, source):
    return {"score": score, "payload": {"authority": authority, "source": source, "status": "current",
                                        "last_verified": "2026-11-12", "content": source}}


def test_canonical_wins_a_close_match_but_not_a_clearly_better_note():
    hits = [_hit(0.80, "derived", "note-close"), _hit(0.78, "canonical", "prd"), _hit(0.95, "derived", "note-best")]
    ranked = [r["source"] for r in server.rank(hits, limit=3)]
    assert ranked == ["note-best", "note-close", "prd"]      # the PRD is 0.17 behind the best: no boost
    close = [_hit(0.80, "derived", "note-close"), _hit(0.78, "canonical", "prd"), _hit(0.81, "derived", "note-best")]
    assert [r["source"] for r in server.rank(close, limit=3)] == ["prd", "note-best", "note-close"]


def test_a_weak_canonical_hit_does_not_jump_a_strong_note():
    hits = [_hit(0.90, "derived", "note-strong"), _hit(0.60, "canonical", "prd-weak")]
    assert [r["source"] for r in server.rank(hits, limit=2)] == ["note-strong", "prd-weak"]


def test_a_canonical_near_tie_goes_to_the_canonical_doc():
    margin = server.CANONICAL_NEAR_TIE_MARGIN
    hits = [_hit(0.90, "derived", "note"), _hit(0.90 - margin, "canonical", "prd")]
    assert [r["source"] for r in server.rank(hits, limit=2)] == ["prd", "note"]


def test_a_canonical_hit_just_outside_the_margin_gets_no_boost():
    outside = 0.90 - server.CANONICAL_NEAR_TIE_MARGIN - 0.01
    hits = [_hit(0.90, "derived", "note"), _hit(outside, "canonical", "prd")]
    assert [r["source"] for r in server.rank(hits, limit=2)] == ["note", "prd"]


def test_only_canonical_results_expose_last_verified():
    canonical, derived = server.rank([_hit(0.9, "canonical", "prd"), _hit(0.8, "derived", "note")], limit=5)
    assert canonical["last_verified"] == "2026-11-12" and "claimed_last_verified" not in canonical
    assert derived["claimed_last_verified"] == "2026-11-12" and "last_verified" not in derived


def test_results_carry_their_provenance_and_the_raw_score():
    result = server.rank([_hit(0.5, "canonical", "prd")], limit=5)[0]
    assert result["authority"] == "canonical"
    assert result["status"] == "current"
    assert result["last_verified"] == "2026-11-12"
    assert result["score"] == 0.5


def test_chunks_indexed_before_metadata_report_unknown():
    result = server.rank([{"score": 0.4, "payload": {"source": "legacy.md", "content": "x"}}], limit=5)[0]
    assert result["authority"] == "unknown" and result["status"] == "unknown"


# --- agent-home copies in the vault are never indexed ---

def test_the_vaults_agent_copies_are_never_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WATCH_DIR", tmp_path)
    skill = tmp_path / "memory-vault" / "agents" / "hermes" / "skills" / "x" / "SKILL.md"
    note = tmp_path / "memory-vault" / "notes" / "Personal" / "Wine" / "Wine Collection.md"
    for path in (skill, note):
        path.parent.mkdir(parents=True)
        path.write_text("text", encoding="utf-8")
    found = [ingest._file_key(p) for p in ingest._iter_supported_files()]
    assert found == ["memory-vault/notes/Personal/Wine/Wine Collection.md"]
    monkeypatch.setattr(ingest, "_embed", lambda chunks: pytest.fail("an agent copy must not be embedded"))
    assert ingest.ingest_path(skill, {}) is False
