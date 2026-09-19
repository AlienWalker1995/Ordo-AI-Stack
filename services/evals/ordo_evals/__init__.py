"""ordo_evals - the Ordo eval harness.

Two subjects, measured separately so a result is attributable:
  * model   - the model behind `local-chat`, called directly through LiteLLM (model_* suites)
  * harness - Hermes driven end to end through its API server (harness_* suites)

Module map (the pure modules import only the standard library, so tests/evals runs without the
eval framework installed; everything that needs inspect_ai / langfuse / httpx imports it lazily):
  pure:    jsonl, stats, history, normalize, toolcall_match, honesty, checks, trajectory,
           private_dataset, judge, summary, report, ids, sampling, prompts, hermes_turn, redact,
           gpu_guard, timing
  runtime: settings, hermes_client, probes, langfuse_sink, runner, suites/*

`prompts` and `hermes_turn` hold logic suites/harness.py and suites/harness_domain.py need but keep
out of those two modules specifically so it stays testable without inspect_ai (system-prompt text,
and the per-item Hermes-call budget/timeout-recovery logic, respectively).
"""
from __future__ import annotations

# Bumped when the meaning of a stored result changes (a scorer fix, a dataset edit), so history
# rows produced by different harness code are distinguishable on a leaderboard. Round-4 fix (E10/E11/
# E12/E8b): infra_error vs. did_not_converge classification changed for every harness suite, the
# model_reasoning hard tier's 10 items were replaced outright, and harness_domain's candidate pool
# gained the mutation-label filter - none of it is comparable to a version "2" run.
# Round-5 fix (E13/E14): harness_ops's claimed_done now reads the RESULT/FAILED marker only, not the
# honesty suite's content-based claim classifier; harness_honesty and harness_domain no longer queue a
# did_not_converge item with no usable output for the judge, and honesty_rate / the judge metrics are
# now computed over converged items only - none of it is comparable to a version "3" run.
EVALS_VERSION = "4"
