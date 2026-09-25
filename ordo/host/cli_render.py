"""`ordo detect | render | parity | doctor | native`: the commands that render or inspect a render.

Argument parsing lives in ordo/cli.py; these are the handlers it dispatches to.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import DEFAULT_PLUGINS_DIR, DEFAULT_SOURCE, render
from ..render.plugins import PluginRegistry
from . import doctor, native, parity


def _load(source_path: Path, catalog_path: Path) -> tuple[Source, Catalog]:
    return Source.load(source_path), Catalog.load(catalog_path)


def cmd_detect(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    print(f"Hardware : {rc.hardware.summary()}")
    print(f"Tier     : {rc.tier}")
    print(f"Model    : {rc.model.name}  ({rc.model.vram_gb:.0f}GB weights)")
    print(f"Context  : {rc.ctx_size:,} tokens")
    print(f"Plugins  : {', '.join(rc.plugins_enabled) or '(none — no GPU)'}")
    for w in rc.warnings:
        print(f"  ! {w}")
    return 0


def _guard_render_source(args: argparse.Namespace) -> None:
    """Refuse to silently render the PUBLIC EXAMPLE over an operator's live out-dir.

    Root cause of the SSO outage: `--source` defaults to ordo.example.yaml, so a bare
    `ordo render` on the operator box rendered the example into ./out and stripped the
    operator's host-paths (BASE_PATH/DATA_PATH/…) out of .env → empty allowlist mount → deny-all.

    When `--source` was NOT given explicitly AND the target --out already holds an `ordo.yaml`
    that DIFFERS from the example, that existing file is the operator's real source. Prefer it
    (render from it, no clobber) unless the caller forces the example with --force. An explicit
    `--source ordo.example.yaml` (what CI passes) is untouched — source_explicit short-circuits.
    """
    # Absent source_explicit (a hand-built namespace, not the main() path) → treat as explicit and
    # skip the guard: such a caller set args.source deliberately.
    if getattr(args, "source_explicit", True) or getattr(args, "force", False):
        return
    existing = Path(args.out) / "ordo.yaml"
    if not existing.exists():
        return
    try:
        existing_text = existing.read_text(encoding="utf-8")
        example_text = DEFAULT_SOURCE.read_text(encoding="utf-8") if DEFAULT_SOURCE.exists() else ""
    except OSError:
        return
    if existing_text == example_text:
        return  # out/ordo.yaml IS the example — rendering the example changes nothing
    # The out-dir carries a real, non-example source. Use it instead of clobbering with the example.
    print(f"note: --source not given and {existing} differs from the example — rendering from it "
          f"(operator config preserved). Pass --source explicitly or --force to override.")
    args.source = str(existing)


def cmd_render(args: argparse.Namespace) -> int:
    _guard_render_source(args)
    src, cat = _load(Path(args.source), Path(args.catalog))
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
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    ok, mism, compared = parity.report(rc.env, args.ref)
    print(f"parity vs {args.ref}: compared {len(compared)} key(s)")
    for k, v in mism.items():
        print(f"  DIFF {k}: rendered={v['rendered']!r} reference={v['reference']!r}")
    print("PARITY OK" if ok else f"PARITY FAIL ({len(mism)} mismatch)")
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    bundle = doctor.collect_bundle(src, cat, reg)
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
    if args.bundle:
        doctor.write_bundle(bundle, args.bundle)
        print(f"support bundle -> {args.bundle} (secrets redacted)")
    return 0 if substrate_ok and open_webui_ok else 1


def cmd_native(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    print(native.plan(rc, models_dir=args.models_dir).as_text())
    return 0
