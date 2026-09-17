"""`python -m ordo_evals <command>`: run, ingest-grades, build-private, report."""
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

    ingest = commands.add_parser("ingest-grades", help="validate judge grades, post them, recompute the summary")
    ingest.add_argument("--run-id", required=True)
    ingest.add_argument("--file", required=True, type=Path, help="grades JSONL (see README: judge workflow)")
    ingest.add_argument("--no-langfuse", action="store_true")

    private = commands.add_parser("build-private", help="build the private model_domain dataset (content never printed)")
    private.add_argument("--source", required=True, choices=["hermes-state"])
    private.add_argument("--n", type=int, default=30)
    private.add_argument("--seed", type=int, default=1234)

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
                          no_langfuse=args.no_langfuse)

    if args.command == "ingest-grades":
        from ordo_evals import runner

        return runner.ingest_grades(settings, run_id=args.run_id, grades_file=args.file, no_langfuse=args.no_langfuse)

    if args.command == "build-private":
        from ordo_evals.jsonl import write_json, write_jsonl
        from ordo_evals.private_dataset import build_private_dataset

        items, stats = build_private_dataset(settings.hermes_state_db, n=args.n, seed=args.seed)
        target = settings.results_dir / "datasets" / "private_domain.jsonl"
        write_jsonl(target, items)
        write_json(target.with_suffix(".meta.json"), stats)
        print(f"[ordo-evals] wrote {stats['selected']} of {stats['candidates']} candidate asks "
              f"(requested {stats['requested']}, seed {stats['seed']}) to {target}")
        return 0 if items else 1

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
