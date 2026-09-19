"""`python -m ordo_evals <command>`: run, ingest-grades, backfill-metrics, build-private,
ingest-labels, report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ordo_evals.suites import SUITE_ORDER, resolve_suites


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ordo_evals", description=(
        "Ordo eval harness: the local-chat model and the Hermes harness, measured separately."))
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run suites and record items, judge queue, summary and history")
    run.add_argument("--suites", required=True, help=f"'all' or a comma-separated list of {', '.join(SUITE_ORDER)}")
    run.add_argument("--run-id", required=True, help="unique id for this run ([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")
    run.add_argument("--limit", type=int, default=None, help="at most N items per suite (smoke runs)")
    run.add_argument("--seed", type=int, default=1234, help="sampling seed (IFEval sample, generation seed)")
    run.add_argument("--no-langfuse", action="store_true", help="do not post datasets, runs or scores to Langfuse")
    run.add_argument("--allow-dirty", action="store_true", help=(
        "run even though the mounted services/evals checkout is dirty or its git provenance is "
        "unknown (not launched via scripts/evals/run.sh); the run is still recorded dirty/unknown"))

    ingest = commands.add_parser("ingest-grades", help="validate judge grades, post them, recompute the summary")
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--file", required=True, type=Path, help="grades JSONL (see README: judge workflow)")
    ingest.add_argument("--no-langfuse", action="store_true")

    backfill = commands.add_parser("backfill-metrics", help=(
        "recompute a completed run's behaviour metrics (E17 stopping, E19 replay) from Hermes's "
        "state.db sessions, then rewrite that run's items, summary and history rows (idempotent)"))
    backfill.add_argument("--run-id", required=True)

    private = commands.add_parser(
        "build-private", help="sample private domain candidates and queue new ones for labelling (content never printed)")
    private.add_argument("--source", required=True, choices=["hermes-state"])
    private.add_argument("--n", type=int, default=30)
    private.add_argument("--seed", type=int, default=1234)

    labels = commands.add_parser(
        "ingest-labels", help="validate and store judge labels for the private domain candidate pool (E2b)")
    labels.add_argument("--file", required=True, type=Path, help="label lines JSONL (see README: labelling workflow)")

    report = commands.add_parser("report", help="print a per-suite metrics table")
    report.add_argument("--run-id", required=True)
    report.add_argument("--compare", default=None, help="a second run id: adds its values and the deltas")
    return parser


def _load_summary(settings, run_id: str) -> dict | None:
    from ordo_evals.runner import run_dir_for

    path = run_dir_for(settings, run_id) / "summary.json"
    if not path.is_file():
        print(f"[ordo-evals] no summary for run {run_id} at {path}", file=sys.stderr)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from ordo_evals.settings import Settings

    settings = Settings.from_env()

    if args.command == "run":
        from ordo_evals import runner

        if args.limit is not None and args.limit <= 0:
            print("[ordo-evals] --limit must be positive", file=sys.stderr)
            return 2
        try:
            suites = resolve_suites(args.suites)
        except ValueError as exc:
            print(f"[ordo-evals] {exc}", file=sys.stderr)
            return 2
        return runner.run(settings, suites=suites, run_id=args.run_id, limit=args.limit, seed=args.seed,
                          no_langfuse=args.no_langfuse, allow_dirty=args.allow_dirty)

    if args.command == "ingest-grades":
        from ordo_evals import runner

        return runner.ingest_grades(settings, run_id=args.run_id, grades_file=args.file, no_langfuse=args.no_langfuse)

    if args.command == "backfill-metrics":
        from ordo_evals import runner

        return runner.backfill_metrics(settings, run_id=args.run_id)

    if args.command == "build-private":
        from ordo_evals import private_dataset as pd
        from ordo_evals.jsonl import read_jsonl, write_json, write_jsonl

        items, stats = pd.build_private_dataset(settings.hermes_state_db, n=args.n, seed=args.seed)
        candidates_path = settings.results_dir / pd.CANDIDATES_FILE
        labels_path = settings.results_dir / pd.LABELS_FILE
        queue_path = settings.results_dir / pd.LABEL_QUEUE_FILE

        existing_candidates = read_jsonl(candidates_path) if candidates_path.is_file() else []
        all_candidates = pd.merge_candidates(existing_candidates, items)
        write_jsonl(candidates_path, all_candidates)

        existing_labels = read_jsonl(labels_path) if labels_path.is_file() else []
        labels = pd.labels_by_id(existing_labels)
        mutations = pd.mutations_by_id(existing_labels)  # E11
        queue = pd.pending_label_queue(all_candidates, labels, mutations)
        write_jsonl(queue_path, queue)
        counts = pd.label_counts(all_candidates, labels)
        mutation_counts = pd.mutation_counts(all_candidates, labels, mutations, pd.LABEL_AGENT_STANDALONE)
        write_json(candidates_path.with_suffix(".meta.json"), {**stats, "counts": counts,
                                                                "agent_standalone_mutation_counts": mutation_counts})

        # E2b/E11: counts only, never content - this sample still needs a judge label (self_contained /
        # agent_standalone / conversation_dependent, and for agent_standalone items, read_only /
        # mutating) before model_domain or harness_domain use it.
        print(f"[ordo-evals] sampled {stats['selected']} of {stats['candidates']} candidate ask(s) "
              f"(requested {stats['requested']}, seed {stats['seed']}); candidate pool now "
              f"{counts['total']} ({counts['self_contained']} self_contained, "
              f"{counts['agent_standalone']} agent_standalone, "
              f"{counts['conversation_dependent']} conversation_dependent, {counts['unlabeled']} unlabeled); "
              f"of the agent_standalone items, {mutation_counts['read_only']} read_only, "
              f"{mutation_counts['mutating']} mutating, {mutation_counts['unlabeled']} not yet mutation-labelled; "
              f"wrote {len(queue)} pending label request(s) to {queue_path}")
        return 0 if all_candidates else 1

    if args.command == "ingest-labels":
        from ordo_evals import private_dataset as pd
        from ordo_evals.jsonl import read_jsonl, write_jsonl

        candidates_path = settings.results_dir / pd.CANDIDATES_FILE
        labels_path = settings.results_dir / pd.LABELS_FILE
        queue_path = settings.results_dir / pd.LABEL_QUEUE_FILE
        if not candidates_path.is_file():
            print(f"[ordo-evals] {candidates_path} not found: run build-private first", file=sys.stderr)
            return 2
        candidates = read_jsonl(candidates_path)
        valid, errors = pd.validate_labels(read_jsonl(args.file), candidates)
        if errors:
            for error in errors:
                print(f"[ordo-evals] INVALID {error}", file=sys.stderr)
            print(f"[ordo-evals] {len(errors)} invalid label line(s); nothing was ingested", file=sys.stderr)
            return 2

        merged = pd.merge_labels(read_jsonl(labels_path) if labels_path.is_file() else [], valid)
        write_jsonl(labels_path, merged)
        labels = pd.labels_by_id(merged)
        mutations = pd.mutations_by_id(merged)  # E11
        write_jsonl(queue_path, pd.pending_label_queue(candidates, labels, mutations))
        counts = pd.label_counts(candidates, labels)
        mutation_counts = pd.mutation_counts(candidates, labels, mutations, pd.LABEL_AGENT_STANDALONE)
        print(f"[ordo-evals] ingested {len(valid)} label(s); candidate pool now {counts['total']} "
              f"({counts['self_contained']} self_contained, {counts['agent_standalone']} agent_standalone, "
              f"{counts['conversation_dependent']} conversation_dependent, {counts['unlabeled']} unlabeled); "
              f"of the agent_standalone items, {mutation_counts['read_only']} read_only, "
              f"{mutation_counts['mutating']} mutating, {mutation_counts['unlabeled']} not yet mutation-labelled")
        return 0

    if args.command == "report":
        from ordo_evals.report import format_report

        current = _load_summary(settings, args.run_id)
        if current is None:
            return 2
        other = _load_summary(settings, args.compare) if args.compare else None
        if args.compare and other is None:
            return 2
        print(format_report(current, other))
        return 0
    return 2
