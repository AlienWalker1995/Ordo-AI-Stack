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

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}

VAULT_MOUNT = "${MEMORY_VAULT_PATH:-${DATA_PATH:-./data}/memory-vault}:/watch/memory-vault:ro"
INGEST_PY = ROOT.parent / "rag-ingestion" / "ingest.py"


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
    assert ri["volumes"][0] == "${DATA_PATH:-./data}/rag-input:/watch"
    # the vault mount is read-only — only the memory-vault MCP writes the vault
    vault = next(v for v in ri["volumes"] if "/watch/memory-vault" in v)
    assert vault.endswith(":ro")


def test_rag_ingestion_contract_otherwise_unchanged():
    c = render(_src(["rag"]), CATALOG, REGISTRY).compose_dict()
    ri = c["services"]["rag-ingestion"]
    # single watch root: the recursive scanner sees the nested vault without extra env
    assert ri["environment"]["WATCH_DIR"] == "/watch"
    assert ri["environment"]["QDRANT_COLLECTION"] == "${RAG_COLLECTION:-documents}"
    assert ri["environment"]["MODEL_GATEWAY_URL"] == "http://llamacpp-embed:8080"


def test_embed_server_batch_fits_a_full_chunk():
    # Embeddings require each input to fit in ONE physical batch; the upstream default
    # (n_ubatch=512) 500s on chunks over 512 tokens — a 400-word markdown chunk routinely is
    # (CONVENTIONS.md = 556 tokens, caught live). Both batch flags must be raised together.
    c = render(_src(["rag"]), CATALOG, REGISTRY).compose_dict()
    cmd = c["services"]["llamacpp-embed"]["command"]
    assert cmd[cmd.index("--batch-size") + 1] == "2048"
    assert cmd[cmd.index("--ubatch-size") + 1] == "2048"
    assert "--embeddings" in cmd


def test_rag_renders_on_cpu_with_vault_mount():
    c = render(_src(["rag"], hardware=P_CPU), CATALOG, REGISTRY).compose_dict()
    assert VAULT_MOUNT in c["services"]["rag-ingestion"]["volumes"]


# ── ingester: hidden-path rule keeps vault internals out of the index ───────
@pytest.fixture()
def ingest(monkeypatch, tmp_path):
    """Load rag-ingestion/ingest.py by path with a stubbed httpx (not a dev dep —
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
