"""ordo CLI — the seed of the one-script.

    ordo detect                 # show detected hardware + what it would pick
    ordo render [--out DIR]     # render config from ordo.yaml into DIR (default ./out)
    ordo doctor                 # sanity checks (catalog integrity, source validity)
    ordo serve                  # run the control-plane HTTP service (ops-controller)

`render` writes to an output dir only (it starts nothing), and `serve`'s Docker backend is
hard-scoped to the ordo project prefix so it only ever touches its own project's containers.
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
from . import doctor, fetch, native, parity, preflight, wizard

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
    print(f"secrets.env.example -> {len(rc.required_secrets)} required key(s): "
          f"{', '.join(rc.required_secrets)}")
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
    """Every locally-present image ref, BOTH as repo:tag AND as repo@sha256:digest.

    A digest-pinned compose image (e.g. `grafana/grafana@sha256:…`) is present in the local
    cache under its RepoDigest, not a repo:tag — so matching only tags falsely reported pinned
    images as 'will pull'. Collect both forms so the preflight image-presence check is accurate.
    """
    import subprocess
    refs: set[str] = set()
    for fmt in ("{{.Repository}}:{{.Tag}}", "{{.Repository}}@{{.Digest}}"):
        try:
            out = subprocess.run(["docker", "images", "--digests", "--format", fmt],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return set()
        for ln in out.stdout.splitlines():
            ln = ln.strip()
            # skip untagged/undigested rows ('<none>' or a bare 'repo@' / 'repo:')
            if ln and "<none>" not in ln and not ln.endswith(("@", ":")):
                refs.add(ln)
    return refs


def cmd_preflight(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    images = None if args.no_images else _local_images()
    go, checks = preflight.run(src, cat, reg, ref_env=args.ref, images_present=images,
                               secrets_env=args.secrets, project=args.project)
    for c in checks:
        mark = "OK " if c.ok else ("!! " if c.blocking else "-- ")
        print(f"  [{mark}] {c.name}: {c.detail}")
    print(f"\n{'GO — safe to cut over' if go else 'NO-GO — resolve the [!!] blocking checks above'}")
    return 0 if go else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    cat = Catalog.load(Path(args.catalog))
    wanted = None if args.all else ([args.model] if args.model else None)
    if not args.all and not args.model:
        # default target: the model the current source resolves to
        wanted = [render(Source.load(Path(args.source)), cat).model.id]
    actions = fetch.plan(cat, wanted, args.models_dir, allow_unverified=args.allow_unverified)
    for a in actions:
        print(f"  [{a.action}] {a.model_id}: {a.reason}")
    if args.plan_only:
        return 0
    blocked = [a for a in actions if a.action == fetch.REFUSE]
    if blocked:
        print(f"\nrefusing {len(blocked)} unpinned model(s); pass --allow-unverified to override")
        return 1
    todo = [a for a in actions if a.action in (fetch.DOWNLOAD, fetch.REDOWNLOAD)]
    for a in todo:  # pragma: no cover - real network downloads
        model = cat.get(a.model_id)
        print(f"fetching {model.id} …")
        result = fetch.fetch_one(model, args.models_dir, allow_unverified=args.allow_unverified)
        print(f"  -> {result.reason}")
    print(f"\n{len(todo)} fetched, {len(actions) - len(todo) - len(blocked)} already present")
    return 0


def cmd_native(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    print(native.plan(rc, models_dir=args.models_dir).as_text())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - binds a socket
    import threading
    import time

    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    src = Source.load(Path(args.source))
    hw = detect()
    cloud_fallback = bool((src.cloud_fallback or {}).get("enabled"))
    sched = Scheduler(hw.primary_vram_gb if hw.has_gpu else 0.0, cloud_fallback=cloud_fallback)
    # Durable lease record (served at GET /jobs/history for the dashboard's orchestration tab).
    # Lives next to the rendered outputs — the same writable /config mount, no extra volume.
    from .lease_history import LeaseHistory

    history = LeaseHistory(Path(args.out) / "lease-history.jsonl")
    broker = Broker(sched, DockerBackend(project=args.project), history=history)
    cp = ControlPlane(Path(args.source), cat, reg, args.out, scheduler=sched, broker=broker,
                      history=history)

    # Resident registration (the missing wiring): the render tells us the LLM's true GPU footprint
    # (weights + KV at the rendered ctx). Register it as an idle-cached resident so a media lease can
    # actually EVICT it — without this the scheduler thinks the whole card is free and never frees
    # the LLM's VRAM (that was the live defect: /status showed free == total). Read from the same
    # render the stack runs, so it can't drift from what `.env` loads.
    if hw.has_gpu:
        rc = render(src, cat, reg)
        resident_gb = rc.resident_vram_gb()
        sched.cache_idle(args.resident_service, resident_gb)
        print(f"[scheduler] registered resident '{args.resident_service}' "
              f"~{resident_gb:.1f}GB (model={rc.model.id}, ctx={rc.ctx_size}) as idle-cached",
              flush=True)

    # Lease clock + self-heal sweep: advance the scheduler's clock by the poll interval and force-
    # complete any lease whose TTL has elapsed (a crashed client can never strand the resident down).
    # sweep_leases() reconciles, which restores an evicted resident once the GPU work has drained.
    def _lease_loop() -> None:
        while True:
            time.sleep(args.lease_poll_seconds)
            try:
                sched.tick(args.lease_poll_seconds)
                swept = broker.sweep_leases()
                if swept:
                    print(f"[scheduler] lease TTL expired for {swept} — resident restored on drain",
                          flush=True)
            except Exception as e:  # noqa: BLE001 — the control plane must survive a sweep hiccup
                print(f"[scheduler] lease sweep error: {e}", flush=True)

    threading.Thread(target=_lease_loop, daemon=True, name="lease-sweep").start()

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
    pget = sub.add_parser("fetch")
    pget.add_argument("model", nargs="?", help="catalog model id (default: the source's model)")
    pget.add_argument("--all", action="store_true", help="fetch every catalog model")
    pget.add_argument("--models-dir", default="./models")
    pget.add_argument("--allow-unverified", action="store_true",
                      help="permit downloading a model with no pinned sha256 (unsafe)")
    pget.add_argument("--plan-only", action="store_true", help="print the plan, download nothing")
    pget.set_defaults(func=cmd_fetch)
    pn = sub.add_parser("native")
    pn.add_argument("--models-dir", default="./models", help="where the GGUF files live natively")
    pn.set_defaults(func=cmd_native)
    pf = sub.add_parser("preflight")
    pf.add_argument("--ref", help="live .env to parity-check against (merge gate)")
    pf.add_argument("--secrets", help="local secrets.env to check required keys against (non-blocking)")
    pf.add_argument("--project", default="ordo")
    pf.add_argument("--no-images", action="store_true", help="skip the docker image-presence check")
    pf.set_defaults(func=cmd_preflight)
    pv = sub.add_parser("serve")
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=9000)
    pv.add_argument("--out", default="out")
    pv.add_argument("--project", default="ordo", help="container project prefix the broker may touch")
    pv.add_argument("--resident-service", default="llamacpp",
                    help="compose service of the resident LLM the scheduler may evict/restore for a lease")
    pv.add_argument("--lease-poll-seconds", type=float, default=10.0,
                    help="how often the scheduler advances its lease clock + sweeps expired leases")
    pv.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
