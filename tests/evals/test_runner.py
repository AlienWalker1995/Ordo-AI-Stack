"""E6: the runner's own RAG-leak safety net (`runner._check_rag_leak`), tested against a fake probe
instead of the full `run()` pipeline (which needs Hermes, Langfuse and every suite stubbed). A
confirmed leak must fail the check; an unreadable probe must only be noted, never fail it - the
runner should never turn "we could not check" into a false positive.

Also E7: the git-provenance gate (`runner._provenance_gate`), and one full-`run()` test that a
refused run writes nothing to disk."""
from __future__ import annotations

from ordo_evals import runner
from ordo_evals.checks import VAULT_EVAL_ROOT, ProbeError
from ordo_evals.settings import Settings


class _FakeLeakProbe:
    def __init__(self, leaked=(), fail=False):
        self._leaked = list(leaked)
        self._fail = fail

    def qdrant_scratch_leak_sources(self):
        if self._fail:
            raise ProbeError("qdrant unreachable")
        return list(self._leaked)


def test_no_leak_returns_empty_and_adds_no_note():
    notes: list[str] = []
    assert runner._check_rag_leak(_FakeLeakProbe(), notes) == []
    assert notes == []


def test_a_leaked_point_fails_the_check_and_is_noted():
    notes: list[str] = []
    leaked = [f"memory-vault/{VAULT_EVAL_ROOT}/run-1/ops-06.md"]
    result = runner._check_rag_leak(_FakeLeakProbe(leaked=leaked), notes)
    assert result == leaked
    assert any("RAG LEAK" in n and VAULT_EVAL_ROOT in n for n in notes)


def test_an_unreadable_probe_is_noted_but_never_fails_the_check():
    notes: list[str] = []
    result = runner._check_rag_leak(_FakeLeakProbe(fail=True), notes)
    assert result == []
    assert any("could not verify" in n for n in notes)


# ── E7: git provenance gate ─────────────────────────────────────────────────────

def test_a_clean_tree_always_proceeds():
    assert runner._provenance_gate(git_dirty=False, allow_dirty=False) is None
    assert runner._provenance_gate(git_dirty=False, allow_dirty=True) is None


def test_a_dirty_tree_is_refused_unless_allow_dirty():
    refusal = runner._provenance_gate(git_dirty=True, allow_dirty=False)
    assert refusal is not None and "dirty" in refusal
    assert runner._provenance_gate(git_dirty=True, allow_dirty=True) is None


def test_unknown_provenance_is_refused_unless_allow_dirty():
    """Not launched via scripts/evals/run.sh (GIT_COMMIT/GIT_DIRTY unset) reads as git_dirty=None -
    refused by default, same as a confirmed-dirty tree, since provenance cannot be trusted either
    way; --allow-dirty still opens the door."""
    refusal = runner._provenance_gate(git_dirty=None, allow_dirty=False)
    assert refusal is not None and "unknown" in refusal
    assert runner._provenance_gate(git_dirty=None, allow_dirty=True) is None


def test_a_refused_run_writes_nothing_to_disk(tmp_path, monkeypatch):
    """The gate runs before run_dir is even created, so a refusal must leave no trace - nothing to
    mistake for a real (if empty) result later."""
    monkeypatch.setenv("EVALS_RESULTS_DIR", str(tmp_path))
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_DIRTY", raising=False)
    settings = Settings.from_env()
    assert settings.git_commit is None and settings.git_dirty is None

    code = runner.run(settings, suites=["model_reasoning"], run_id="should-not-exist", limit=1,
                      seed=1, no_langfuse=True)

    assert code == 4
    assert list(tmp_path.iterdir()) == []
