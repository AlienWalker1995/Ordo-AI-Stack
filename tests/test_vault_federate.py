"""Tests for the vault-federation reconciliation core (pure logic)."""

import importlib.util
import pathlib
import sys

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
