"""ordo CLI — the seed of the one-script.

    ordo detect                 # show detected hardware + what it would pick
    ordo render [--out DIR]     # render config from ordo.yaml into DIR (default ./out)
    ordo doctor                 # sanity checks (catalog integrity, source validity)

Never touches a running stack — render writes to an output dir only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .catalog import Catalog
from .config import Source
from .hardware import detect
from .render import render

HERE = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = HERE / "ordo.example.yaml"
DEFAULT_CATALOG = HERE / "catalog" / "models.yaml"


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


def cmd_render(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    rc.write(args.out)
    print(f"Rendered -> {args.out}/  (model={rc.model.id}, ctx={rc.ctx_size:,})")
    # the drift-proof invariant, shown every render:
    m = rc.manifest()["derived"]
    consistent = len({str(v) for v in m.values()}) == 1
    print(f"ctx consistency across .env / hermes / model-gateway: "
          f"{'OK' if consistent else 'MISMATCH'} ({m['env.LLAMACPP_CTX_SIZE']})")
    return 0 if consistent else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    src, cat = _load(Path(args.source), Path(args.catalog))
    print(f"source '{args.source}': valid")
    unpinned = [m.id for m in cat.models if not m.sha256]
    if unpinned:
        print(f"! {len(unpinned)} catalog model(s) have no sha256 (download will refuse "
              f"unless --allow-unverified): {', '.join(unpinned)}")
    hw = detect()
    print(f"detected: {hw.summary()}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ordo", description="Ordo config render engine")
    p.add_argument("--source", default=str(DEFAULT_SOURCE))
    p.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect").set_defaults(func=cmd_detect)
    pr = sub.add_parser("render")
    pr.add_argument("--out", default="out")
    pr.set_defaults(func=cmd_render)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
