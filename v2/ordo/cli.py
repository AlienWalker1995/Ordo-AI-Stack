"""ordo CLI — the seed of the one-script.

    ordo detect                 # show detected hardware + what it would pick
    ordo render [--out DIR]     # render config from ordo.yaml into DIR (default ./out)
    ordo doctor                 # sanity checks (catalog integrity, source validity)
    ordo serve                  # run the control-plane HTTP service (ops-controller)

Never touches a running stack — render writes to an output dir only, and `serve`'s Docker
backend is hard-scoped to the ordo-v2 project prefix.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .broker import Broker, DockerBackend
from .catalog import Catalog
from .config import Source
from .control import ControlPlane
from .hardware import detect
from .plugins import PluginRegistry
from .render import DEFAULT_PLUGINS_DIR, render
from .scheduler import Scheduler
from . import doctor, native, parity, preflight, wizard

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


def cmd_setup(args: argparse.Namespace) -> int:
    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    interactive = not args.yes and sys.stdin.isatty()
    out = wizard.run(cat, reg, args.out, interactive=interactive,
                     answers={} if not interactive else None)
    print(f"Wrote {out} — now run: ordo render")
    return 0


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
    if args.bundle:
        doctor.write_bundle(bundle, args.bundle)
        print(f"support bundle -> {args.bundle} (secrets redacted)")
    return 0


def _local_images() -> set[str]:  # pragma: no cover - shells to docker
    import subprocess
    try:
        out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                             capture_output=True, text=True, timeout=30)
        return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def cmd_preflight(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    images = None if args.no_images else _local_images()
    go, checks = preflight.run(src, cat, reg, ref_env=args.ref, images_present=images,
                               project=args.project)
    for c in checks:
        mark = "OK " if c.ok else ("!! " if c.blocking else "-- ")
        print(f"  [{mark}] {c.name}: {c.detail}")
    print(f"\n{'GO — safe to cut over' if go else 'NO-GO — resolve the [!!] blocking checks above'}")
    return 0 if go else 1


def cmd_native(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    print(native.plan(rc, models_dir=args.models_dir).as_text())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - binds a socket
    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    hw = detect()
    cloud_fallback = bool((Source.load(Path(args.source)).cloud_fallback or {}).get("enabled"))
    sched = Scheduler(hw.primary_vram_gb if hw.has_gpu else 0.0, cloud_fallback=cloud_fallback)
    broker = Broker(sched, DockerBackend(project=args.project))
    cp = ControlPlane(Path(args.source), cat, reg, args.out, scheduler=sched, broker=broker)
    print(f"ops-controller on {args.host}:{args.port} (project={args.project}, "
          f"{sched.total_vram_gb:.0f}GB GPU) — Ctrl-C to stop")
    cp.serve(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ordo", description="Ordo config render engine")
    p.add_argument("--source", default=str(DEFAULT_SOURCE))
    p.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect").set_defaults(func=cmd_detect)
    pr = sub.add_parser("render")
    pr.add_argument("--out", default="out")
    pr.set_defaults(func=cmd_render)
    ps = sub.add_parser("setup")
    ps.add_argument("--out", default="ordo.yaml")
    ps.add_argument("--yes", action="store_true", help="non-interactive (accept detected)")
    ps.set_defaults(func=cmd_setup)
    pp = sub.add_parser("parity")
    pp.add_argument("--ref", required=True, help="reference .env to compare the render against")
    pp.set_defaults(func=cmd_parity)
    pd = sub.add_parser("doctor")
    pd.add_argument("--bundle", help="write a sanitized support bundle to this path")
    pd.set_defaults(func=cmd_doctor)
    pn = sub.add_parser("native")
    pn.add_argument("--models-dir", default="./models", help="where the GGUF files live natively")
    pn.set_defaults(func=cmd_native)
    pf = sub.add_parser("preflight")
    pf.add_argument("--ref", help="live .env to parity-check against (merge gate)")
    pf.add_argument("--project", default="ordo-v2")
    pf.add_argument("--no-images", action="store_true", help="skip the docker image-presence check")
    pf.set_defaults(func=cmd_preflight)
    pv = sub.add_parser("serve")
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=9000)
    pv.add_argument("--out", default="out")
    pv.add_argument("--project", default="ordo-v2", help="container project prefix the broker may touch")
    pv.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
