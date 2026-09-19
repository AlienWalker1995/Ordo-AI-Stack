"""Orchestration for `run`, `ingest-grades` and `backfill-metrics`.

Layout of one run (all under /results, outside git):
    runs/<run-id>/inspect/           Inspect .eval logs (one per suite)
    runs/<run-id>/items.jsonl        one record per item: input, output, scores, trace_id, errors
    runs/<run-id>/judge_queue.jsonl  items awaiting the judge (see judge.py for the format)
    runs/<run-id>/grades.jsonl       every validated grade ingested so far
    runs/<run-id>/summary.json       per-suite metrics {value, n, ci95, denominator} + identities +
                                     the run-level contention block + skipped suites
    history.jsonl                    one row per (run, suite, metric) across all runs (history.py)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from ordo_evals import EVALS_VERSION, gpu_guard, history, judge, redact, summary, timing
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


def _gpu_preflight_reason(probes: Any) -> str | None:
    """E15 (round-6 fix): a refusal reason, or None if the GPU is clear. Used both before any suite
    runs (same "leaves no trace on disk" guarantee as _provenance_gate above) and cheaply between
    suites, so a run started while the GPU was leased - or one that becomes leased mid-run - never
    finishes producing numbers that silently mix the GPU and CPU deployments (gpu_guard.py). An
    ops-controller that cannot be read is treated the same as leased: refusing on a status this
    package cannot verify is the same conservative default `_provenance_gate` uses for unknown git
    provenance, not a guess that everything is fine."""
    try:
        status = probes.ops_status()
    except ProbeError as exc:
        return f"could not check GPU lease state ({exc}); ops-controller must be reachable"
    leased, detail = gpu_guard.gpu_lease_state(status)
    if not leased:
        return None
    return (f"GPU is currently leased ({detail}); a leased GPU can silently serve local-chat from "
            "the slow CPU fallback deployment (see services/evals/README.md's run-validity section)")


def _backend_integrity_reason(served_models_by_subject: dict[str, set[str]]) -> str | None:
    """E15: a run whose model-suite items (or whose harness-suite items) show more than one distinct
    served backend cannot be trusted - part of it measured a different deployment than the rest.
    Checked per SUBJECT bucket, never by comparing a raw model-suite served_model (a llama.cpp
    completion response's own `model` field, e.g. a gguf path) against a harness-suite one
    (gpu_guard's coarser gpu/cpu-fallback sentinel - the best signal available for a Hermes turn, see
    gpu_guard.py's module docstring): the two vocabularies are only meaningful within themselves."""
    for subject, models in sorted(served_models_by_subject.items()):
        if len(models) > 1:
            return f"{subject} suites saw more than one served backend this run: {sorted(models)}"
    return None


def run(settings: Settings, *, suites: list[str], run_id: str, limit: int | None, seed: int,
        no_langfuse: bool, allow_dirty: bool = False, probes: Any = None) -> int:
    # Checked first, before even the lazy imports below (some pull in the heavy optional Inspect-AI
    # dependency): a refused run must never need to load a suite to be refused, and must leave no
    # trace on disk (no run_dir).
    refusal = _provenance_gate(settings.git_dirty, allow_dirty)
    if refusal:
        _log(f"refusing to start run {run_id}: {refusal}")
        return 4

    from ordo_evals.probes import LiveProbes

    # `probes` is injectable (tests only; production always constructs the real LiveProbes) so the
    # E15 GPU-lease checks below can be exercised against a fake ops-controller response.
    probes = probes if probes is not None else LiveProbes(
        vault_dir=settings.vault_dir, ops_controller_url=settings.ops_controller_url,
        n8n_url=settings.n8n_url, qdrant_url=settings.qdrant_url, qdrant_collection=settings.qdrant_collection)

    # E15 (round-6 fix): refused before run_dir exists, same guarantee as the provenance gate above -
    # a run that starts while the GPU is already leased can spend its whole duration silently served
    # by the CPU fallback deployment with nothing in the results to show it (the iteration-4 failure).
    # Checked before the suites.common import below (which pulls in the heavy optional Inspect-AI
    # dependency, same reasoning as the provenance gate's own import ordering) so a refusal here is
    # exactly as cheap as one from the provenance gate.
    gpu_refusal = _gpu_preflight_reason(probes)
    if gpu_refusal:
        _log(f"refusing to start run {run_id}: {gpu_refusal}")
        return 5

    from ordo_evals.hermes_client import HermesClient
    from ordo_evals.suites.common import SuiteContext

    run_dir = run_dir_for(settings, run_id)
    if (run_dir / "summary.json").exists() or (run_dir / "items.jsonl").exists():
        _log(f"run {run_id} already exists at {run_dir}; choose a new --run-id")
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)
    sink = make_sink(settings, no_langfuse)
    ctx = SuiteContext(run_id=run_id, seed=seed, limit=limit, settings=settings, run_dir=run_dir, probes=probes)
    served_model = _served_model(probes, settings, ctx.notes)
    ctx.served_model = served_model  # E15: threaded to the harness suites via suites/harness.py's call_hermes
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
        "integrity": None,  # E15: set to "backend_changed" below if the run turns out untrustworthy
    }
    # E15: distinct served_model values seen so far, kept separately per SUBJECT (see
    # _backend_integrity_reason for why model-suite and harness-suite values are never compared to
    # each other) - accumulated across the whole run, not reset per suite, so a change first visible
    # in a LATER suite is still caught.
    served_models_by_subject: dict[str, set[str]] = {"model": set(), "harness": set()}
    # Every item this run has collected so far, kept only to recompute the run-level `contention`
    # block (round-7 fix) after each suite - the same items already written to items.jsonl.
    run_items: list[dict[str, Any]] = []
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
            # E15: per-item tokens/second + the suite-local slow-item flag, computed before the items
            # are written so the flag is part of the persisted record, not a report-time afterthought.
            timing.annotate_tokens_per_second(items)
            run_items.extend(items)
            # Round-7 fix: how loaded the box was, run-wide, from the per-item rates just computed.
            run_summary["contention"] = timing.contention(run_items)
            append_jsonl(run_dir / "items.jsonl", items)
            if queue:
                append_jsonl(run_dir / "judge_queue.jsonl", queue)
            sink.ensure_dataset(suite, module.DESCRIPTION)
            for item in items:
                sink.record_item(run_id, item)
                _post_item_scores(sink, run_id, item)
            subject = SUBJECTS[suite]
            served_models = sorted({i["served_model"] for i in items if i.get("served_model")})
            served_models_by_subject[subject].update(served_models)
            block = summary.suite_summary(suite, items, [])
            block.update({
                "subject": subject,
                "model": _harness_model(items, served_model, settings) if subject == "harness" else served_model,
                "harness": harness_identity if subject == "harness" else None,
                "judge_queued": len(queue),
                "served_models": served_models,  # E15: the distinct set this suite actually saw
            })
            ids = sampled_item_ids(items, limit)
            if ids is not None:
                block["sampled_item_ids"] = ids
            run_summary["suites"][suite] = block
            write_json(run_dir / "summary.json", run_summary)
            history.append_rows(settings.results_dir / "history.jsonl",
                                summary.history_rows(run_summary, suites=[suite]))
            _log(f"{suite}: {len(items)} items, {len(queue)} queued for the judge")

            # E15: never finish a run whose numbers cannot be trusted. Checked after EVERY suite, not
            # only harness ones - the model suites are exactly as exposed to LiteLLM's fallback - so a
            # mid-run backend change is caught as soon as its evidence exists, and the GPU is
            # re-checked live (cheaply - one ops-controller GET) so a suite that hasn't shown the
            # drift YET is still stopped before it starts one that would.
            integrity_reason = _backend_integrity_reason(served_models_by_subject) or _gpu_preflight_reason(probes)
            if integrity_reason:
                run_summary["integrity"] = "backend_changed"
                run_summary["integrity_detail"] = integrity_reason
                write_json(run_dir / "summary.json", run_summary)
                # Retroactively stamp every row this run has ALREADY appended to history.jsonl
                # (including this suite's own, written moments ago with integrity still null) - a
                # partially-marked run would let a later query see some of its rows as trustworthy,
                # exactly the drift this whole fix exists to close.
                history.replace_run_suites(settings.results_dir / "history.jsonl", run_id,
                                           list(run_summary["suites"]), summary.history_rows(run_summary))
                _log(f"run {run_id} ABORTED after {suite}: {integrity_reason}")
                break
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
    if run_summary.get("integrity"):
        _log(f"run {run_id} INVALID: {run_summary.get('integrity_detail')}")
        return 6
    if rag_leak:
        _log(f"run {run_id} FAILED: RAG-leak safety check found {len(rag_leak)} Qdrant point(s) rooted "
             f"under {VAULT_EVAL_ROOT}/ (see summary.json notes); this is an infra error, not a suite result")
        return 3
    _log(f"run {run_id} complete: {run_dir}")
    return 0


def _item_window_s(item: dict[str, Any]) -> float | None:
    """The seconds the harness waited for this item (E22), or None when neither duration was recorded.

    `trajectory.wall_time_s` is the Hermes call's own wall clock (hermes_turn.call_hermes) and is the
    window the run itself read. `time_s` is Inspect's total for the sample - very slightly wider (it
    includes the item's setup and scoring) - and is used only when an older run has no wall_time_s.
    """
    for value in ((item.get("metadata") or {}).get("trajectory") or {}).get("wall_time_s"), item.get("time_s"):
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def backfill_metrics(settings: Settings, *, run_id: str) -> int:
    """Recompute a COMPLETED run's behaviour metrics (E17 stopping, E19 replay) from state.db.

    The round-7 metrics are read from the ordered tool calls and results of a Hermes session, which
    state.db still holds for runs that have already happened: this gives the stopping-rule experiment
    its baselines without spending GPU time re-running them. It rewrites that run's items.jsonl,
    summary.json (every suite's metrics recomputed from the stored items and grades, plus the
    run-level `contention` block) and its rows in history.jsonl, and it is idempotent - the same
    state.db and the same items produce the same numbers.

    Only the BEHAVIOUR fields are written onto an item's trajectory (trajectory.BEHAVIOUR_FIELDS);
    every run-time measurement recorded during the run (wall_time_s, served_model, token counts) is
    left exactly as it was. A session that is no longer in state.db is recorded as
    `behaviour_known: false` (nulls), never guessed.

    E22 (round-10 fix): every session is read bounded to the ITEM'S OWN WINDOW - the seconds the
    harness waited for it (`trajectory.wall_time_s`, falling back to the sample's `time_s`). Before
    this, the session was read as it stood at backfill time, so a did_not_converge item picked up
    every tool call Hermes made after the harness gave up: `hon-07-missing-workflow` in
    loop5-20260919-1600 recorded 41 tool calls and was backfilled with
    `calls_after_first_negative: 60`, a number larger than the item's own call count and one that
    changed with WHEN the backfill happened to run. Bounded, a backfilled reading is the one the run
    would have recorded. An item with neither duration recorded is read unbounded and says so
    (`window_s: null` on its trajectory), rather than being silently mixed in.
    """
    from ordo_evals import hermes_turn, trajectory

    run_dir = run_dir_for(settings, run_id)
    items_path, summary_path = run_dir / "items.jsonl", run_dir / "summary.json"
    for path in (items_path, summary_path):
        if not path.is_file():
            _log(f"{path} not found: nothing to backfill")
            return 2

    items = read_jsonl(items_path)
    grades_path = run_dir / "grades.jsonl"
    grades = read_jsonl(grades_path) if grades_path.is_file() else []
    read_count = missing_count = 0
    for item in items:
        if SUBJECTS.get(item["suite"]) != "harness":
            continue  # a model suite has no Hermes session to read
        metadata = item.get("metadata") or {}
        item["metadata"] = metadata
        session_id = metadata.get("session_id") or hermes_turn.session_id_for(run_id, item["suite"], item["item_id"])
        window_s = _item_window_s(item)
        try:
            session = trajectory.session_metrics(settings.hermes_state_db, session_id, run_id=run_id,
                                                 window_s=window_s)
        except Exception as exc:  # an unreadable state.db is recorded as unknown, never as "no negative"
            _log(f"WARNING could not read session {session_id}: {type(exc).__name__}: {exc}")
            session = {"found": False}
        traj = metadata.get("trajectory") or {}
        metadata["trajectory"] = traj
        if session.get("found"):
            traj.update({field: session[field] for field in trajectory.BEHAVIOUR_FIELDS})
            read_count += 1
        else:
            traj.update(trajectory.behaviour_unknown())
            missing_count += 1
    # The per-item token rates the contention block aggregates are themselves derived from stored
    # data (E15, round 6), so a run that predates that fix can have them computed here too - per
    # SUITE, exactly as runner.run does it, because the slow-item flag is relative to the suite's own
    # median and a run-wide median would mean something different.
    for suite in sorted({i["suite"] for i in items}):
        timing.annotate_tokens_per_second([i for i in items if i["suite"] == suite])
    write_jsonl(items_path, items)

    run_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for suite, block in run_summary["suites"].items():
        block["metrics"] = summary.suite_summary(suite, [i for i in items if i["suite"] == suite],
                                                 grades)["metrics"]
    run_summary["contention"] = timing.contention(items)
    write_json(summary_path, run_summary)
    history.replace_run_suites(settings.results_dir / "history.jsonl", run_id, list(run_summary["suites"]),
                               summary.history_rows(run_summary))
    _log(f"backfilled {run_id}: {read_count} harness session(s) read, {missing_count} missing from state.db; "
         f"summary and history recomputed")
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
