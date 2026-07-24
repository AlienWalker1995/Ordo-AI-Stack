"""Model provisioning: mandatory checksum verification, refuse-unpinned, reject-corrupt, idempotent."""
import hashlib
from pathlib import Path

import pytest

from ordo import fetch
from ordo.catalog import Catalog, Model

HELLO = b"hello world"
HELLO_SHA = hashlib.sha256(HELLO).hexdigest()


def _model(sha=HELLO_SHA, file="m.gguf", source="https://example/m.gguf"):
    return Model.from_dict({"id": "m", "file": file, "source": source, "sha256": sha,
                            "requires": {"vram_gb": 1}})


def _writes(data):
    def dl(url, dest):
        Path(dest).write_bytes(data)
    return dl


def test_sha256_file(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(HELLO)
    assert fetch.sha256_file(p) == HELLO_SHA


def test_classify_states(tmp_path):
    m = _model()
    assert fetch.classify(m, tmp_path) == "missing"
    (tmp_path / "m.gguf").write_bytes(HELLO)
    assert fetch.classify(m, tmp_path) == "verified"
    (tmp_path / "m.gguf").write_bytes(b"corrupt")
    assert fetch.classify(m, tmp_path) == "mismatch"
    assert fetch.classify(_model(sha=None), tmp_path) == "present-unverified"


def test_plan_refuses_unpinned_missing_without_flag(tmp_path):
    cat = Catalog([_model(sha=None)])
    acts = fetch.plan(cat, None, tmp_path, allow_unverified=False)
    assert acts[0].action == fetch.REFUSE
    # with the override, it becomes a download
    assert fetch.plan(cat, None, tmp_path, allow_unverified=True)[0].action == fetch.DOWNLOAD


def test_plan_skips_verified_and_flags_mismatch(tmp_path):
    cat = Catalog([_model()])
    (tmp_path / "m.gguf").write_bytes(HELLO)
    assert fetch.plan(cat, None, tmp_path)[0].action == fetch.OK
    (tmp_path / "m.gguf").write_bytes(b"bad")
    assert fetch.plan(cat, None, tmp_path)[0].action == fetch.REDOWNLOAD


def test_fetch_verifies_and_keeps(tmp_path):
    a = fetch.fetch_one(_model(), tmp_path, downloader=_writes(HELLO))
    assert a.action == fetch.DOWNLOAD
    assert (tmp_path / "m.gguf").read_bytes() == HELLO


def test_fetch_rejects_and_deletes_corrupt(tmp_path):
    with pytest.raises(ValueError, match="checksum mismatch"):
        fetch.fetch_one(_model(), tmp_path, downloader=_writes(b"tampered"))
    assert not (tmp_path / "m.gguf").exists()      # corrupt weights never left on disk


def test_fetch_refuses_null_sha_without_flag(tmp_path):
    with pytest.raises(ValueError, match="no sha256"):
        fetch.fetch_one(_model(sha=None), tmp_path, downloader=_writes(HELLO))


def test_fetch_is_idempotent_and_offline(tmp_path):
    (tmp_path / "m.gguf").write_bytes(HELLO)       # already present + verified
    called = []
    a = fetch.fetch_one(_model(), tmp_path, downloader=lambda u, d: called.append(u))
    assert a.action == fetch.OK and called == []   # no network call — offline-ready
