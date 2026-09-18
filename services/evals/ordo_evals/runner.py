"""Orchestration for `run` and `ingest-grades`.

Layout of one run (all under /results, outside git):
    runs/<run-id>/inspect/           Inspect .eval logs (one per suite)
    runs/<run-id>/items.jsonl        one record per item: input, output, scores, trace_id, errors
    runs/<run-id>/judge_queue.jsonl  items awaiting the judge (see judge.py for the format)
    runs/<run-id>/grades.jsonl       every validated grade ingested so far
    runs/<run-id>/summary.json       per-suite metrics {value, n, ci95} + identities + skipped suites
    history.jsonl                    one row per (run, suite, metric) across all runs (history.py)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ordo_evals import EVALS_VERSION, history, judge, redact, summary
from ordo_evals.checks import VAULT_EVAL_ROOT, ProbeError
from ordo_evals.ids import validate_run_id
from ordo_evals.jsonl import append_jsonl, read_jsonl, write_json, write_jsonl
from ordo_evals.langfuse_sink import LangfuseSink, NullSink
from ordo_evals.sampling import sampled_item_ids
from ordo_evals.settings import Settings
from ordo_evals.suites import SUBJECTS, load


def _log(message: str) -> None:
    print(f"[ordo-evals] {message}", flush=True)


def make_sink(settings: Settings, no_langfuse: bool) -> LangfuseSink | NullSink:
    if no_langfuse:
        return NullSink()
    return LangfuseSink(host=settings.langfuse_host, public_key=settings.langfuse_public_key,
                        secret_key=settings.langfuse_secret_key)


def run_dir_for(settings: Settings, run_id: str) -> Path:
    return settings.results_dir / "runs" / validate_run_id(run_id)


def _served_model(probes: Any, settings: Settings, notes: list[str]) -> str:
    """The catalog id of the model behind the alias (ops-controller), so history rows stay attributable
    when the alias is re-pointed. Falls back to the alias itself, with a note."""
    try:
        model_id = probes.ops_status().get("manifest", {}).get("model", {}).get("id")
    except ProbeError as exc:
        notes.append(f"could not resolve the served model from ops-controller ({exc}); recorded the alias")
        return settings.model_name
    return str(model_id) if model_id else settings.model_name


def _harness_model(items: list[dict[str, Any]], served_model: str, settings: Settings) -> str:
    """Which model Hermes ran on: the served model when Hermes used the local alias, else what it used."""
    used = {((i.get("metadata") or {}).get("trajectory") or {}).get("model") for i in items}
    used.discard(None)
    if not used or used == {settings.model_name}:
        return served_model
    return ",".join(sorted(str(u) for u in used))


def _post_item_scores(sink: Any, run_id: str, item: dict[str, Any]) -> None:
    for name, value in item["scores"].items():
        sink.post_score(run_id, item["suite"], item["item_id"], item["trace_id"], name, value)


def _check_rag_leak(probes: Any, notes: list[str]) -> list[str]:
    """E6 safety net: even with rag-ingestion's hidden-path exclusion of VAULT_EVAL_ROOT, confirm no
    Qdrant point's `source` is actually rooted under it. Ground truth being unreadable (rag/qdrant
    not deployed, network hiccup) is recorded as a note, never as a run failure - only a CONFIRMED
    leak fails the run, so this can never turn "we could not check" into a false failure."""
    try:
        leaked = probes.qdrant_scratch_leak_sources()
    except ProbeError as exc:
        notes.append(f"could not verify no RAG leak under {VAULT_EVAL_ROOT}/ ({exc})")
        return []
    if leaked:
        notes.append(f"RAG LEAK: {len(leaked)} Qdrant point(s) ingested from under {VAULT_EVAL_ROOT}/, "
                     f"e.g. {leaked[0]!r}")
    return leaked


def _provenance_gate(git_dirty: bool | None, allow_dirty: bool) -> str | None:
    """E7: a refusal reason, or None if the run may proceed. Checked before ANY suite runs (and
    before run_dir even exists) so a dirty or unprovenanced checkout can never produce a summary or
    history row that looks clean. `git_dirty` (settings.git_dirty) is False only when
    scripts/evals/run.sh confirmed a clean `services/evals` tree with the real host git; True means
    it found uncommitted changes there; None means the run was not launched through that wrapper at
    all (GIT_COMMIT/GIT_DIRTY unset) and provenance cannot be trusted either way."""
    if git_dirty is False:
        return None
    if allow_dirty:
        return None
    if git_dirty is True:
        return ("the mounted services/evals tree has uncommitted changes (dirty); pass --allow-dirty "
                "to run anyway (recorded dirty: true in summary.json and every history row)")
    return ("git provenance is unknown (GIT_COMMIT/GIT_DIRTY not set - invoke via "
            "scripts/evals/run.sh, not `docker compose run` directly); pass --allow-dirty to run "
            "anyway (recorded with a null commit/dirty)")


def run(settings: Settings, *, suites: list[str], run_id: str, limit: int | None, seed: int,
        no_langfuse: bool, allow_dirty: bool = False) -> int:
    # Checked first, before even the lazy imports below (some pull in the heavy optional Inspect-AI
    # dependency): a refused run must never need to load a suite to be refused, and must leave no
    # trace on disk (no run_dir).
    refusal = _provenance_gate(settings.git_dirty, allow_dirty)
    if refusal:
        _log(f"refusing to start run {run_id}: {refusal}")
        return 4

    from ordo_evals.hermes_client import HermesClient
    from ordo_evals.probes import LiveProbes
    from ordo_evals.suites.common import SuiteContext

    run_dir = run_dir_for(settings, run_id)
    if (run_dir / "summary.json").exists() or (run_dir / "items.jsonl").exists():
        _log(f"run {run_id} already exists at {run_dir}; choose a new --run-id")
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    sink = make_sink(settings, no_langfuse)
    probes = LiveProbes(vault_dir=settings.vault_dir, ops_controller_url=settings.ops_controller_url,
                        n8n_url=settings.n8n_url, qdrant_url=settings.qdrant_url,
                        qdrant_collection=settings.qdrant_collection)
    ctx = SuiteContext(run_id=run_id, seed=seed, limit=limit, settings=settings, run_dir=run_dir, probes=probes)
    served_model = _served_model(probes, settings, ctx.notes)
    harness_identity = None
    if any(SUBJECTS[s] == "harness" for s in suites) and settings.hermes_api_key:
        ctx.hermes = HermesClient(settings.hermes_api_url, settings.hermes_api_key, settings.hermes_timeout_s)
        try:
            harness_identity = f"hermes-agent@{asyncio.run(ctx.hermes.version())}"
            ctx.hermes_model_name = asyncio.run(ctx.hermes.model_id())
        except Exception as exc:  # recorded per suite below; the model suites can still run
            ctx.notes.append(f"Hermes API server unreachable: {type(exc).__name__}: {exc}")
            ctx.hermes = None

    run_summary: dict[str, Any] = {
        "run_id": run_id, "ts": history.utc_now_iso(), "evals_version": EVALS_VERSION, "seed": seed,
        "limit": limit, "model_alias": settings.model_name, "served_model": served_model,
        "langfuse": sink.enabled, "suites": {}, "skipped": {}, "notes": ctx.notes,
        "commit": settings.git_commit, "dirty": settings.git_dirty,
    }
    rag_leak: list[str] = []
    try:
        for suite in suites:
            module = load(suite)
            reason = module.unavailable_reason(ctx)
            if reason is None and SUBJECTS[suite] == "harness" and ctx.hermes is None:
                reason = "Hermes API server unreachable (see notes)"
            if reason:
                _log(f"{suite}: SKIPPED - {reason}")
                run_summary["skipped"][suite] = reason
                write_json(run_dir / "summary.json", run_summary)
                continue
            _log(f"{suite}: running")
            items, queue = module.run(ctx)
            # E12: redact secret-shaped substrings BEFORE anything below writes or posts this data -
            # items.jsonl, judge_queue.jsonl and every Langfuse call (record_item, post_score) all
            # read from these same (now-redacted) lists, so this is the one place a leak is caught.
            items = [redact.redact_item(item) for item in items]
            queue = [redact.redact_queue_entry(entry) for entry in queue]
            append_jsonl(run_dir / "items.jsonl", items)
            if queue:
                append_jsonl(run_dir / "judge_queue.jsonl", queue)
            sink.ensure_dataset(suite, module.DESCRIPTION)
            for item in items:
                sink.record_item(run_id, item)
                _post_item_scores(sink, run_id, item)
            subject = SUBJECTS[suite]
            block = summary.suite_summary(suite, items, [])
            block.update({
                "subject": subject,
                "model": _harness_model(items, served_model, settings) if subject == "harness" else served_model,
                "harness": harness_identity if subject == "harness" else None,
                "judge_queued": len(queue),
            })
            ids = sampled_item_ids(items, limit)
            if ids is not None:
                block["sampled_item_ids"] = ids
            run_summary["suites"][suite] = block
            write_json(run_dir / "summary.json", run_summary)
            history.append_rows(settings.results_dir / "history.jsonl",
                                summary.history_rows(run_summary, suites=[suite]))
            _log(f"{suite}: {len(items)} items, {len(queue)} queued for the judge")
    finally:
        if any(SUBJECTS[s] == "harness" for s in suites):
            rag_leak = _check_rag_leak(probes, ctx.notes)
            if rag_leak:
                run_summary["rag_leak_sources"] = rag_leak
            try:
                probes.cleanup_run(run_id)
            except Exception as exc:  # never mask the run's own outcome
                print(f"[ordo-evals] WARNING vault cleanup of {VAULT_EVAL_ROOT}/{run_id} failed: {exc}",
                      file=sys.stderr)
            # ctx.notes / rag_leak_sources are only known after the last per-suite write_json call
            # above, so persist the final summary once more here.
            write_json(run_dir / "summary.json", run_summary)
        sink.flush()
    if rag_leak:
        _log(f"run {run_id} FAILED: RAG-leak safety check found {len(rag_leak)} Qdrant point(s) rooted "
             f"under {VAULT_EVAL_ROOT}/ (see summary.json notes); this is an infra error, not a suite result")
        return 3
    _log(f"run {run_id} complete: {run_dir}")
    return 0


def ingest_grades(settings: Settings, *, run_id: str, grades_file: Path, no_langfuse: bool) -> int:
    run_dir = run_dir_for(settings, run_id)
    queue_path, items_path, summary_path = (run_dir / "judge_queue.jsonl", run_dir / "items.jsonl",
                                            run_dir / "summary.json")
    for path in (queue_path, items_path, summary_path):
        if not path.is_file():
            _log(f"{path} not found: nothing to ingest into")
            return 2
    valid, errors = judge.validate_grades(read_jsonl(grades_file), read_jsonl(queue_path))
    if errors:
        for error in errors:
            print(f"[ordo-evals] INVALID {error}", file=sys.stderr)
        _log(f"{len(errors)} invalid grade line(s); nothing was ingested")
        return 2
    # E12: a judge's rationale can quote the graded reply; redact before it is written to grades.jsonl
    # or posted as a Langfuse score comment, same as items.jsonl/judge_queue.jsonl above.
    valid = [dict(grade, rationale=redact.redact_secrets(grade["rationale"])) for grade in valid]

    items = read_jsonl(items_path)
    trace_ids = {(i["suite"], i["item_id"]): i["trace_id"] for i in items}
    sink = make_sink(settings, no_langfuse)
    try:
        for grade in valid:
            sink.post_score(run_id, grade["suite"], grade["item_id"], trace_ids[(grade["suite"], grade["item_id"])],
                            f"judge.{grade['criterion']}", grade["score"], comment=grade["rationale"])
    finally:
        sink.flush()

    grades_path = run_dir / "grades.jsonl"
    all_grades = judge.merge_grades(read_jsonl(grades_path) if grades_path.is_file() else [], valid)
    write_jsonl(grades_path, all_grades)

    run_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    affected = sorted({g["suite"] for g in valid})
    for suite in affected:
        block = run_summary["suites"][suite]
        suite_items = [i for i in items if i["suite"] == suite]
        recomputed = summary.suite_summary(suite, suite_items, all_grades)
        block["metrics"] = recomputed["metrics"]
        block["judge_graded"] = len({g["item_id"] for g in all_grades if g["suite"] == suite})
    write_json(summary_path, run_summary)
    history.replace_run_suites(settings.results_dir / "history.jsonl", run_id, affected,
                               summary.history_rows(run_summary, suites=affected))
    _log(f"ingested {len(valid)} grade(s) for {affected}; summary and history recomputed")
    return 0
