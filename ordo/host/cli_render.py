"""`ordo detect | render | parity | doctor | native`: the commands that render or inspect a render.

Argument parsing lives in ordo/cli.py; these are the handlers it dispatches to.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import DEFAULT_PLUGINS_DIR, DEFAULT_SOURCE, render
from ..render.plugins import PluginRegistry
from . import doctor, native, parity


def _load(source_path: Path, catalog_path: Path) -> tuple[Source, Catalog]:
    return Source.load(source_path), Catalog.load(catalog_path)


def announce_implicit_source(args: argparse.Namespace) -> None:
    """Say on stderr which source a command read when the operator did not name one (ordo/cli.py
    resolves it: the live <out>/ordo.yaml, else the example). stdout stays the command's own output."""
    # Absent source_explicit (a hand-built namespace, not the main() path): the caller chose the source.
    if not getattr(args, "source_explicit", True):
        print(f"source: {args.source} (--source not given)", file=sys.stderr)


def load_args(args: argparse.Namespace) -> tuple[Source, Catalog]:
    """The source and catalog a command's arguments name, announcing an implicit source."""
    announce_implicit_source(args)
    return _load(Path(args.source), Path(args.catalog))


def cmd_detect(args: argparse.Namespace) -> int:
    src, cat = load_args(args)
    rc = render(src, cat)
    print(f"Hardware : {rc.hardware.summary()}")
    print(f"Tier     : {rc.tier}")
    print(f"Model    : {rc.model.name}  ({rc.model.vram_gb:.0f}GB weights)")
    print(f"Context  : {rc.ctx_size:,} tokens")
    print(f"Plugins  : {', '.join(rc.plugins_enabled) or '(none — no GPU)'}")
    for w in rc.warnings:
        print(f"  ! {w}")
    return 0


def _force_example_source(args: argparse.Namespace) -> None:
    """`ordo render --force` without --source renders the public example even though --out holds the
    operator's ordo.yaml (which ordo/cli.py would otherwise have resolved). An explicit --source wins."""
    if getattr(args, "force", False) and not getattr(args, "source_explicit", True):
        args.source = str(DEFAULT_SOURCE)


def cmd_render(args: argparse.Namespace) -> int:
    _force_example_source(args)
    src, cat = load_args(args)
    try:
        rc = render(src, cat)
    except ValueError as e:
        # e.g. an explicit plugin whose required site keys are unset: fail here, naming them,
        # rather than at `docker compose` interpolation. Nothing is written.
        print(f"error: {e}")
        return 1
    rc.write(args.out)
    print(f"Rendered -> {args.out}/  (model={rc.model.id}, ctx={rc.ctx_size:,})")
    print(f"secrets.env.example -> {len(rc.required_secrets)} required key(s): "
          f"{', '.join(rc.required_secrets)}")
    # the drift-proof invariant, shown every render:
    m = rc.manifest()["derived"]
    consistent = len({str(v) for v in m.values()}) == 1
    print(f"ctx consistency across .env / hermes / model-gateway: "
          f"{'OK' if consistent else 'MISMATCH'} ({m['env.LLAMACPP_CTX_SIZE']})")
    return 0 if consistent else 1


def cmd_parity(args: argparse.Namespace) -> int:
    src, cat = load_args(args)
    rc = render(src, cat)
    ok, mism, compared = parity.report(rc.env, args.ref)
    print(f"parity vs {args.ref}: compared {len(compared)} key(s)")
    for k, v in mism.items():
        print(f"  DIFF {k}: rendered={v['rendered']!r} reference={v['reference']!r}")
    print("PARITY OK" if ok else f"PARITY FAIL ({len(mism)} mismatch)")
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    src, cat = load_args(args)
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    rc = render(src, cat, reg)
    bundle = doctor.collect_bundle(src, cat, reg, rendered=rc)
    print(f"source '{args.source}': valid")
    print(f"detected: {bundle['hardware']}")
    print(f"sizing  : tier={bundle['sizing']['tier']} model={bundle['sizing']['model']} "
          f"ctx={bundle['sizing']['ctx_size']:,}")
    unpinned = bundle["catalog"]["unpinned_sha256"]
    if unpinned:
        print(f"! {len(unpinned)} catalog model(s) have no sha256 (download refuses unless "
              f"--allow-unverified): {', '.join(unpinned)}")
    substrate_ok, substrate_line = doctor.substrate_check(args.project)
    print(substrate_line)
    open_webui_ok, open_webui_line = doctor.open_webui_check(args.project)
    print(open_webui_line)
    alerting_ok, alerting_line = doctor.alerting_check(rc.compose_dict(), args.out)
    print(alerting_line)
    if args.bundle:
        doctor.write_bundle(bundle, args.bundle)
        print(f"support bundle -> {args.bundle} (secrets redacted)")
    return 0 if substrate_ok and open_webui_ok and alerting_ok else 1


def cmd_native(args: argparse.Namespace) -> int:
    src, cat = load_args(args)
    rc = render(src, cat)
    print(native.plan(rc, models_dir=args.models_dir).as_text())
    return 0
