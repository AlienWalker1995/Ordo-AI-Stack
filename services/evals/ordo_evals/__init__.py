"""ordo_evals - the Ordo eval harness.

Two subjects, measured separately so a result is attributable:
  * model   - the model behind `local-chat`, called directly through LiteLLM (model_* suites)
  * harness - Hermes driven end to end through its API server (harness_* suites)

Module map (the pure modules import only the standard library, so tests/evals runs without the
eval framework installed; everything that needs inspect_ai / langfuse / httpx imports it lazily):
  pure:    jsonl, stats, history, normalize, toolcall_match, honesty, checks, trajectory,
           private_dataset, judge, summary, report, ids, sampling
  runtime: settings, hermes_client, probes, langfuse_sink, runner, suites/*
"""
from __future__ import annotations

# Bumped when the meaning of a stored result changes (a scorer fix, a dataset edit), so history
# rows produced by different harness code are distinguishable on a leaderboard.
EVALS_VERSION = "2"
