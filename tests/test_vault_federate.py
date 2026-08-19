"""Tests for the vault-federation reconciliation core (pure logic)."""

import importlib.util
import os
import pathlib
import sys
import time

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "hermes" / "scripts" / "vault_federate.py"
_spec = importlib.util.spec_from_file_location("vault_federate", _MOD_PATH)
vf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vf
_spec.loader.exec_module(vf)


def _h(text: str) -> str:
    return vf.content_hash(text)


def make(volume=None, vault=None, synced=None, locked=False):
    return vf.PairState(volume_text=volume, vault_text=vault, synced_hash=synced, volume_locked=locked)


def test_locked_volume_skips_everything():
    pair = make(volume="a", vault="b", synced=_h("a"), locked=True)
    assert vf.decide(pair, is_soul=False) == "skip_locked"


def test_both_missing_is_noop():
    assert vf.decide(make(), is_soul=False) == "noop"


def test_identical_content_is_noop():
    pair = make(volume="same", vault="same", synced=None)
    assert vf.decide(pair, is_soul=False) == "noop"


def test_never_synced_volume_only_pushes():
    pair = make(volume="agent memory", vault=None, synced=None)
    assert vf.decide(pair, is_soul=False) == "push"


def test_vault_only_pulls_to_volume():
    pair = make(volume=None, vault="operator wrote this", synced=None)
    assert vf.decide(pair, is_soul=False) == "pull"


def test_operator_edit_pulls():
    pair = make(volume="v1", vault="v2-operator", synced=_h("v1"))
    assert vf.decide(pair, is_soul=False) == "pull"


def test_agent_edit_pushes():
    pair = make(volume="v2-agent", vault="v1", synced=_h("v1"))
    assert vf.decide(pair, is_soul=False) == "push"


def test_both_changed_operator_wins_with_preservation():
    pair = make(volume="v2-agent", vault="v2-operator", synced=_h("v1"))
    assert vf.decide(pair, is_soul=False) == "conflict_pull"


def test_never_synced_divergence_is_conflict_pull():
    pair = make(volume="agent version", vault="operator version", synced=None)
    assert vf.decide(pair, is_soul=False) == "conflict_pull"


def test_vault_deletion_of_synced_pair_quarantines():
    pair = make(volume="v1", vault=None, synced=_h("v1"))
    assert vf.decide(pair, is_soul=False) == "quarantine"


def test_vault_deletion_of_soul_restores_instead():
    pair = make(volume="soul text", vault=None, synced=_h("soul text"))
    assert vf.decide(pair, is_soul=True) == "restore_soul"


def test_vault_deletion_with_local_edits_still_quarantines_non_soul():
    pair = make(volume="v2-agent", vault=None, synced=_h("v1"))
    assert vf.decide(pair, is_soul=False) == "quarantine"


def _mk_pair(tmp_path, name="mem", is_soul=False, volume=None, vault=None, lock_age=None):
    vol = tmp_path / "hermes" / f"{name}.md"
    note = tmp_path / "notes" / "Ordo" / "Context Files" / f"{name}.md"
    vol.parent.mkdir(parents=True, exist_ok=True)
    note.parent.mkdir(parents=True, exist_ok=True)
    if volume is not None:
        vol.write_text(volume, encoding="utf-8")
    if vault is not None:
        note.write_text(vault, encoding="utf-8")
    if lock_age is not None:
        lock = vol.with_name(vol.name + ".lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - lock_age
        os.utime(lock, (old, old))
    return vf.Pair(name=name, volume=vol, vault=note, is_soul=is_soul)


def _dirs(tmp_path):
    q = tmp_path / "q"
    c = tmp_path / "conflicts"
    q.mkdir(exist_ok=True)
    c.mkdir(exist_ok=True)
    return q, c


def test_sync_pull_overwrites_volume_and_updates_manifest(tmp_path):
    pair = _mk_pair(tmp_path, volume="old", vault="operator new")
    manifest = {"mem": {"hash": vf.content_hash("old")}}
    q, c = _dirs(tmp_path)
    applied = vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="20260819T000000Z")
    assert applied == "pull"
    assert pair.volume.read_text(encoding="utf-8") == "operator new"
    assert manifest["mem"]["hash"] == vf.content_hash("operator new")


def test_sync_push_writes_vault_note(tmp_path):
    pair = _mk_pair(tmp_path, volume="agent new", vault="old")
    manifest = {"mem": {"hash": vf.content_hash("old")}}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="x") == "push"
    assert pair.vault.read_text(encoding="utf-8") == "agent new"


def test_conflict_pull_preserves_agent_version(tmp_path):
    pair = _mk_pair(tmp_path, volume="agent v2", vault="operator v2")
    manifest = {"mem": {"hash": vf.content_hash("v1")}}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="20260819T010203Z") == "conflict_pull"
    assert pair.volume.read_text(encoding="utf-8") == "operator v2"
    preserved = list(c.glob("mem-*.md"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "agent v2"


def test_quarantine_moves_volume_file(tmp_path):
    pair = _mk_pair(tmp_path, volume="v1")
    manifest = {"mem": {"hash": vf.content_hash("v1")}}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="x") == "quarantine"
    assert not pair.volume.exists()
    assert (q / "mem.md").read_text(encoding="utf-8") == "v1"
    assert "mem" not in manifest


def test_soul_restore_recreates_note(tmp_path):
    pair = _mk_pair(tmp_path, name="SOUL", is_soul=True, volume="soul text")
    manifest = {"SOUL": {"hash": vf.content_hash("soul text")}}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="x") == "restore_soul"
    assert pair.volume.read_text(encoding="utf-8") == "soul text"
    assert pair.vault.read_text(encoding="utf-8") == "soul text"


def test_fresh_lock_skips(tmp_path):
    pair = _mk_pair(tmp_path, volume="a", vault="b", lock_age=10)
    manifest = {}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="x") == "skip_locked"


def test_stale_lock_does_not_skip(tmp_path):
    pair = _mk_pair(tmp_path, volume="same", vault="same", lock_age=3600)
    manifest = {}
    q, c = _dirs(tmp_path)
    assert vf.sync_pair(pair, manifest, quarantine_dir=q, conflicts_dir=c, now_utc="x") == "noop"


def test_manifest_round_trip_atomic(tmp_path):
    p = tmp_path / "state" / "vault-federation.json"
    vf.save_manifest(p, {"a": {"hash": "h"}})
    assert vf.load_manifest(p) == {"a": {"hash": "h"}}
    assert vf.load_manifest(tmp_path / "missing.json") == {}


def test_default_pairs_names_and_soul_flag(tmp_path):
    pairs = vf.default_pairs(tmp_path / "hh", tmp_path / "vn")
    by_name = {p.name: p for p in pairs}
    assert set(by_name) == {"SOUL", "MEMORY", "USER"}
    assert by_name["SOUL"].is_soul is True
    assert by_name["MEMORY"].vault.name == "Agent Context.md"
    assert by_name["USER"].vault.name == "User Profile.md"
