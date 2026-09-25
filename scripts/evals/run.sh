#!/usr/bin/env bash
set -euo pipefail

# Canonical entry point for `python -m ordo_evals` inside the evals one-shot container (E7).
#
# The evals image has no git binary by design (services/evals/README.md's provenance section):
# services/evals is bind-mounted LIVE from the checkout (${BASE_PATH}/services/evals:/app:ro), so
# the container itself cannot know which commit or working-tree state actually produced a run. This
# script computes that on the HOST, with the real `git`, and passes it through as GIT_COMMIT /
# GIT_DIRTY. `python -m ordo_evals run` reads them (ordo_evals.settings.Settings) and refuses to
# start (exit 4) on a dirty or unprovenanced services/evals tree unless `--allow-dirty` is among the
# forwarded args (see ordo_evals.runner._provenance_gate). A run started with --allow-dirty is still
# recorded dirty: true (or commit: null) in summary.json and every history.jsonl row, so it can be
# excluded from baselines later.
#
# This does NOT catch edits made to services/evals AFTER a run starts (suites load lazily): the
# dirty check is a start-of-run snapshot, not continuous monitoring. Do not edit services/evals
# while a run you care about is in flight.
#
# Usage (from anywhere in the repo; forwards every argument to `python -m ordo_evals`):
#   scripts/evals/run.sh run --suites all --run-id 2026-09-20-nightly
#   scripts/evals/run.sh run --suites harness_ops --run-id smoke --limit 3 --allow-dirty
#   scripts/evals/run.sh build-private --source hermes-state --n 30 --seed 1234
#   scripts/evals/run.sh report --run-id 2026-09-20-nightly --compare 2026-09-13-nightly
#   scripts/evals/run.sh backfill-metrics --run-id 2026-09-13-nightly
#
# Requires: `ordo render --out out` already done (out/docker-compose.yml, out/.env and
# out/secrets.env current) and the `evals` plugin enabled in ordo.yaml's plugins list.

if [ "$#" -eq 0 ]; then
    echo "usage: scripts/evals/run.sh <ordo_evals-subcommand> [args...]  (e.g. run --suites all --run-id <id>)" >&2
    exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
OUT_DIR="$REPO_ROOT/out"

if [ ! -f "$OUT_DIR/docker-compose.yml" ]; then
    echo "scripts/evals/run.sh: $OUT_DIR/docker-compose.yml not found - run 'ordo render --out out' first" >&2
    exit 1
fi

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [ -n "$(git -C "$REPO_ROOT" status --porcelain -- services/evals)" ]; then
    GIT_DIRTY=1
else
    GIT_DIRTY=0
fi
export GIT_COMMIT GIT_DIRTY

cd "$OUT_DIR"
exec docker compose -p ordo --profile evals --env-file .env --env-file secrets.env --env-file secret-files.env run --rm evals "$@"
