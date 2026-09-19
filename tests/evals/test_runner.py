"""E6: the runner's own RAG-leak safety net (`runner._check_rag_leak`), tested against a fake probe
instead of the full `run()` pipeline (which needs Hermes, Langfuse and every suite stubbed). A
confirmed leak must fail the check; an unreadable probe must only be noted, never fail it - the
runner should never turn "we could not check" into a false positive.

Also E7: the git-provenance gate (`runner._provenance_gate`), and one full-`run()` test that a
refused run writes nothing to disk.

Also E15 (round-6 fix): the GPU-lease preflight gate and the run-level backend-integrity check.
The full-`run()` tests here use model-only suites (both subjects "model") with `runner.load`
monkeypatched to fake suite modules and a fake ops-controller probe, so the real orchestration path
(provenance gate, GPU preflight, the per-suite loop, summary.json + history.jsonl writes, the exit
code) is exercised end to end without needing Hermes, Langfuse, or the inspect_ai-dependent real
suites (unavailable in this test environment - see tests/evals's package docstring convention)."""
from __future__ import annotations

import dataclasses
import json
import sys
import types

from ordo_evals import runner
from ordo_evals.checks import VAULT_EVAL_ROOT, ProbeError
from ordo_evals.jsonl import read_jsonl
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


# ── E15 (round-6 fix): the GPU-lease preflight gate ─────────────────────────────

IDLE_STATUS = {"manifest": {"model": {"id": "declared-gpu-model"}},
              "gpu": {"state": "idle", "running": [], "queued": [], "evicted_residents": {}}}
LEASED_STATUS = {"manifest": {"model": {"id": "declared-gpu-model"}},
                 "gpu": {"state": "busy", "running": [{"id": "gate-comfyui"}], "queued": [], "evicted_residents": {}}}


class _FakeProbesForRun:
    """Only `ops_status()` is needed: these tests use model-only suites, so runner.run never reaches
    the harness-only cleanup/RAG-leak path that would need the rest of the Probes protocol."""

    def __init__(self, status):
        self._status = status

    def ops_status(self):
        return self._status


class _UnreachableProbes:
    def ops_status(self):
        raise ProbeError("ops-controller down")


def test_gpu_preflight_allows_when_idle():
    assert runner._gpu_preflight_reason(_FakeProbesForRun(IDLE_STATUS)) is None


def test_gpu_preflight_refuses_when_leased():
    reason = runner._gpu_preflight_reason(_FakeProbesForRun(LEASED_STATUS))
    assert reason is not None and "leased" in reason


def test_gpu_preflight_refuses_when_ops_controller_is_unreachable():
    """Ground truth unreadable is refused, not treated as "assume idle" - the same conservative
    default _provenance_gate applies to unknown git provenance."""
    reason = runner._gpu_preflight_reason(_UnreachableProbes())
    assert reason is not None and "could not check" in reason


def test_run_refuses_to_start_when_the_gpu_is_already_leased(tmp_path, monkeypatch):
    """Refused before run_dir exists, same guarantee as the provenance gate - and it must never even
    load a suite (checked by making `load` blow up if called)."""
    monkeypatch.setenv("EVALS_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("GIT_DIRTY", "0")
    settings = Settings.from_env()

    def _must_not_load(name):
        raise AssertionError(f"suite {name!r} must never be loaded when the GPU is leased")

    monkeypatch.setattr(runner, "load", _must_not_load)

    code = runner.run(settings, suites=["model_reasoning"], run_id="leased-refusal", limit=None, seed=1,
                      no_langfuse=True, probes=_FakeProbesForRun(LEASED_STATUS))

    assert code == 5
    assert list(tmp_path.iterdir()) == []


# ── E15: the run-level backend-integrity check ──────────────────────────────────

def test_backend_integrity_passes_with_one_backend_per_subject():
    assert runner._backend_integrity_reason({"model": {"gpu-a"}, "harness": {"gpu-a"}}) is None


def test_backend_integrity_passes_with_empty_subjects():
    assert runner._backend_integrity_reason({"model": set(), "harness": set()}) is None


def test_backend_integrity_fails_when_a_subject_saw_two_backends():
    reason = runner._backend_integrity_reason({"model": {"gpu-a", "cpu-fallback (gpu leased)"}, "harness": set()})
    assert reason is not None and "model suites" in reason


def test_backend_integrity_never_compares_across_subjects():
    """A model-suite served_model (a raw gguf path) and a harness-suite one (gpu_guard's coarser
    sentinel) use different vocabularies on purpose (gpu_guard.py's module docstring) - one distinct
    value in EACH bucket must never be read as two backends."""
    reason = runner._backend_integrity_reason({"model": {"/models/gpu.gguf"}, "harness": {"declared-gpu-model"}})
    assert reason is None


# ── E15: full run() - a single backend passes, a mixed one is marked and non-zero ───────────────

class _FakeSuiteModule:
    def __init__(self, description, items):
        self.DESCRIPTION = description
        self._items = items

    def unavailable_reason(self, ctx):
        return None

    def run(self, ctx):
        return self._items, []


def _fake_item(suite, subject, item_id, served_model, scores):
    return {"run_id": "r1", "suite": suite, "subject": subject, "item_id": item_id, "input": "i",
           "output": "o", "target": None, "served_model": served_model, "scores": scores, "metadata": {},
           "trace_id": "t" * 32, "error": None, "infra_error": False, "usage": {}, "time_s": 1.0}


def _clean_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALS_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("GIT_DIRTY", "0")
    return Settings.from_env()


def _install_fake_suite_context(monkeypatch):
    """`runner.run` does `from ordo_evals.suites.common import SuiteContext` - the real module pulls
    in inspect_ai (unavailable in this test environment; see tests/evals's package docstring
    convention), so a fake `ordo_evals.suites.common` is pre-registered in `sys.modules` with a
    plain-dataclass SuiteContext carrying exactly the fields runner.run sets on it. Python's import
    machinery checks sys.modules before executing a module's source, so the real (inspect_ai-
    importing) file is never touched."""
    fake_common = types.ModuleType("ordo_evals.suites.common")

    @dataclasses.dataclass
    class _FakeSuiteContext:
        run_id: str
        seed: int
        limit: int | None
        settings: object
        run_dir: object
        probes: object = None
        hermes: object = None
        hermes_model_name: str = ""
        served_model: str = ""
        notes: list = dataclasses.field(default_factory=list)

    fake_common.SuiteContext = _FakeSuiteContext
    monkeypatch.setitem(sys.modules, "ordo_evals.suites.common", fake_common)


def test_run_with_a_single_backend_passes_and_records_no_integrity_marker(tmp_path, monkeypatch):
    _install_fake_suite_context(monkeypatch)
    settings = _clean_settings(tmp_path, monkeypatch)
    modules = {
        "model_reasoning": _FakeSuiteModule("reasoning", [
            _fake_item("model_reasoning", "model", "a", "gpu-a", {"correct": True, "format_ok": True})]),
        "model_toolcall": _FakeSuiteModule("toolcall", [
            _fake_item("model_toolcall", "model", "b", "gpu-a", {"correct": True})]),
    }
    monkeypatch.setattr(runner, "load", lambda name: modules[name])

    code = runner.run(settings, suites=["model_reasoning", "model_toolcall"], run_id="single-backend",
                      limit=None, seed=1, no_langfuse=True, probes=_FakeProbesForRun(IDLE_STATUS))

    assert code == 0
    run_summary = json.loads((runner.run_dir_for(settings, "single-backend") / "summary.json")
                            .read_text(encoding="utf-8"))
    assert run_summary["integrity"] is None
    assert run_summary["suites"]["model_reasoning"]["served_models"] == ["gpu-a"]
    assert run_summary["suites"]["model_toolcall"]["served_models"] == ["gpu-a"]
    rows = read_jsonl(settings.results_dir / "history.jsonl")
    assert rows and all(r["integrity"] is None for r in rows)


def test_run_with_mixed_backends_marks_integrity_and_returns_a_nonzero_exit(tmp_path, monkeypatch):
    """E15: the iteration-4 shape - one suite served by the GPU model, a later one (within the same
    run) served by the CPU fallback - must be caught and marked, not silently averaged in."""
    _install_fake_suite_context(monkeypatch)
    settings = _clean_settings(tmp_path, monkeypatch)
    modules = {
        "model_reasoning": _FakeSuiteModule("reasoning", [
            _fake_item("model_reasoning", "model", "a", "gpu-a", {"correct": True, "format_ok": True})]),
        "model_toolcall": _FakeSuiteModule("toolcall", [
            _fake_item("model_toolcall", "model", "b", "cpu-fallback (gpu leased)", {"correct": True})]),
    }
    monkeypatch.setattr(runner, "load", lambda name: modules[name])

    code = runner.run(settings, suites=["model_reasoning", "model_toolcall"], run_id="mixed-backend",
                      limit=None, seed=1, no_langfuse=True, probes=_FakeProbesForRun(IDLE_STATUS))

    assert code == 6
    run_summary = json.loads((runner.run_dir_for(settings, "mixed-backend") / "summary.json")
                            .read_text(encoding="utf-8"))
    assert run_summary["integrity"] == "backend_changed"
    assert "model suites" in run_summary["integrity_detail"]
    rows = read_jsonl(settings.results_dir / "history.jsonl")
    assert rows and all(r["integrity"] == "backend_changed" for r in rows)
