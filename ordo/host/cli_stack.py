"""`ordo preflight | fetch | up | recreate | apply | build`: the commands that act on the rendered stack
from the host. Argument parsing lives in ordo/cli.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ..render import models_volume, served_models, stack
from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import DEFAULT_PLUGINS_DIR, render
from ..render.image_tags import load_record
from ..render.plugins import PluginRegistry
from . import apply, bringup, cli_render, cli_secrets, fetch, images, parity, preflight


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
    src, cat = cli_render._load(Path(args.source), Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    present = None if args.no_images else _local_images()
    image_tags = load_record(args.out)
    go, checks = preflight.run(src, cat, reg, ref_env=args.ref, images_present=present,
                               secrets_env=args.secrets, project=args.project,
                               image_tags=image_tags)
    if not args.no_host:
        # The whole configured stack (every profile), exactly what `ordo up --all` would start.
        rc = render(src, cat, reg)
        services = rc.compose_dict(project=args.project, image_tags=image_tags)["services"]
        facts = preflight.gather_host_facts(preflight.published_ports(services, rc.env), args.project,
                                            str(Path(args.out).resolve()))
        host = preflight.host_checks(services, rc.env, facts, secret_keys=rc.required_secrets,
                                     optional_secrets=rc.optional_secrets, secrets_path=None,
                                     model_files=preflight.model_files(services, rc.env, cat))
        checks += host
        go = go and all(c.ok for c in host if c.blocking)
    _print_checks(checks)
    print(f"\n{'GO: ready to bring up' if go else 'NO-GO: resolve the [!!] blocking checks above'}")
    return 0 if go else 1


def _volume_fetch_targets(args: argparse.Namespace, cat: Catalog) -> list | None:
    """The catalog entries `ordo fetch` provisions into the volume, or None (an error was printed).

    Default: every file the rendered stack in --out loads (chat, projector, CPU fallback, embedder)."""
    if args.all:
        return cat.entries()
    if args.model:
        model = cat.get_entry(args.model)
        if model is None:
            print(f"no catalog entry '{args.model}'", file=sys.stderr)
            return None
        return cat.files_of(model)          # its weights, and its projector when one is pinned
    out = Path(args.out)
    try:
        doc = stack.load_compose(out.resolve().as_posix())
    except (OSError, ValueError) as e:
        print(f"cannot read the rendered stack in {out} ({e}); render first, or name a catalog id",
              file=sys.stderr)
        return None
    env = parity.load_env(str(out / ".env")) if (out / ".env").exists() else {}
    targets = []
    for need in served_models.model_files(doc, env):
        model = cat.by_file(need.file)
        if model is None:
            print(f"  [no catalog entry] {need.file} ({need.service}"
                  f"{', optional' if need.optional else ''}): copy it into the volume by hand")
        elif model not in targets:
            targets.append(model)
    return targets


def cmd_fetch(args: argparse.Namespace) -> int:
    cat = Catalog.load(Path(args.catalog))
    if args.models_dir is None:
        return _fetch_into_volume(args, cat)
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
        model = cat.get_entry(a.model_id)
        print(f"fetching {model.id} …")
        result = fetch.fetch_one(model, args.models_dir, allow_unverified=args.allow_unverified)
        print(f"  -> {result.reason}")
    print(f"\n{len(todo)} fetched, {len(actions) - len(todo) - len(blocked)} already present")
    return 0


def _fetch_into_volume(args: argparse.Namespace, cat: Catalog) -> int:  # pragma: no cover - docker
    """`ordo fetch` (the default): download into the models volume the stack reads, verifying every
    file, present ones included."""
    targets = _volume_fetch_targets(args, cat)
    if targets is None:
        return 1
    refused = [reason for model in targets if (reason := fetch.refusal(model, args.allow_unverified))]
    if refused:
        print("refusing:\n  " + "\n  ".join(refused), file=sys.stderr)
        return 1
    runner = models_volume.DockerRunner()
    volume = models_volume.volume_name(args.project)
    exists = models_volume.volume_exists(runner, volume)
    present = (models_volume.files_in_volume(runner, volume) or set()) if exists else set()
    for model in targets:
        print(f"  [{'present' if model.file in present else 'missing'}] {model.id}: {model.file}")
    if args.plan_only or not targets:
        return 0
    if not exists and not fetch.create_volume(runner, volume, args.project):
        print(f"cannot create the {volume} volume", file=sys.stderr)
        return 1
    secrets_path = Path(args.out) / "secrets.env"
    secrets = parity.load_env(str(secrets_path)) if secrets_path.exists() else {}
    code = fetch.fetch_into_volume(targets, project=args.project, secrets=secrets, runner=runner)
    if code == 0:
        print(f"\n{len(targets)} model file(s) in place and verified in {volume}")
    return code


def _print_checks(checks: list[preflight.Check]) -> None:
    for c in checks:
        mark = "OK " if c.ok else ("!! " if c.blocking else "-- ")
        print(f"  [{mark}] {c.name}: {c.detail}")


def _host_preflight(out_dir: str, project: str, services: list[str], *, whole_stack: bool,
                    with_profiles: bool, catalog_path: str) -> bool:
    """Run the host checks for what this `ordo up` would start. False = refuse the bring-up.

    A render that cannot be read (or names an unknown service) is left to bring_up, which reports it."""
    out = Path(out_dir)
    try:
        doc = stack.load_compose(out.resolve().as_posix())
    except (OSError, ValueError):
        return True
    if any(name not in (doc.get("services") or {}) for name in services):
        return True
    starting = stack.starting_services(doc, services, whole_stack=whole_stack, with_profiles=with_profiles)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    secret_keys = manifest.get("required_secrets")
    if secret_keys is None:  # a render from before the manifest listed them
        example = out / "secrets.env.example"
        secret_keys = list(parity.load_env(str(example))) if example.exists() else []
    env = parity.load_env(str(out / ".env")) if (out / ".env").exists() else {}
    facts = preflight.gather_host_facts(preflight.published_ports(starting, env), project, str(out.resolve()))
    checks = preflight.host_checks(
        starting, env, facts, secret_keys=secret_keys,
        optional_secrets=manifest.get("optional_secrets", []), secrets_path=str(out / "secrets.env"),
        model_files=preflight.model_files(starting, env, Catalog.load(catalog_path)))
    failed = [c for c in checks if c.blocking and not c.ok]
    if failed or any(not c.ok for c in checks):
        _print_checks(checks)
    if failed:
        print("\nNO-GO: fix the [!!] lines above and re-run (or pass --no-preflight to skip these checks).")
        return False
    return True


def cmd_up(args: argparse.Namespace) -> int:
    forms = int(args.all) + int(args.core) + int(bool(args.services))
    if forms != 1:
        print("ordo up: give exactly one of --all, --core or SERVICE...", file=sys.stderr)
        return 1
    whole_stack = args.all or args.core
    out = Path(args.out)
    if not args.dry_run and cli_secrets._prepare_secrets(args, out) != 0:
        return 1
    if not args.dry_run and not args.no_preflight:
        if not _host_preflight(args.out, args.project, args.services, whole_stack=whole_stack,
                               with_profiles=not args.core, catalog_path=args.catalog):
            return 1
    rc = bringup.bring_up(args.out, args.project, args.services, whole_stack=whole_stack,
                          with_profiles=not args.core, force_recreate=False, dry_run=args.dry_run,
                          build=not args.no_build,
                          models_catalog=None if args.no_fetch else Path(args.catalog))
    if rc == 0 and not args.dry_run and (whole_stack or "dashboard" in args.services):
        cli_secrets._print_dashboard_sign_in(out)
    return rc


def cmd_recreate(args: argparse.Namespace) -> int:
    if bool(args.services) == bool(args.reading):
        print("ordo recreate: give exactly one of SERVICE... or --reading KEY...", file=sys.stderr)
        return 1
    services = list(args.services)
    if args.reading:
        # The services that read the keys, from the render: after a secret rotation this is the
        # set that must be recreated (a restart keeps the old environment).
        try:
            doc = stack.load_compose(Path(args.out).resolve().as_posix())
        except (OSError, yaml.YAMLError) as e:
            print(f"cannot read {args.out}/{stack.COMPOSE_FILE} ({e}); render first: "
                  "ordo --source out/ordo.yaml render --out out", file=sys.stderr)
            return 1
        services = stack.readers_of(doc, args.reading)
        if not services:
            print(f"no rendered service reads {', '.join(args.reading)}; nothing to recreate")
            return 0
        print(f"services reading {', '.join(args.reading)}: {' '.join(services)}")
    return bringup.bring_up(args.out, args.project, services, whole_stack=False,
                            with_profiles=True, force_recreate=True, dry_run=args.dry_run,
                            build=not args.no_build)


def cmd_apply(args: argparse.Namespace) -> int:
    """`ordo apply`: the deploy, in its one correct order (ordo/host/apply.py)."""
    out = Path(args.out)
    source = cli_secrets._source_path(args)
    if not source.exists():
        print(f"no operator source at {source}: run `ordo init`, or pass --source", file=sys.stderr)
        return 1
    host = apply.RealHost(
        source_path=source, catalog_path=Path(args.catalog), out=out, project=args.project,
        preflight=lambda services: _host_preflight(args.out, args.project, services, whole_stack=False,
                                                   with_profiles=True, catalog_path=args.catalog),
        materialize_secrets=lambda: cli_secrets._prepare_secrets(args, out),
        doctor=lambda: cli_render.cmd_doctor(argparse.Namespace(source=str(source), catalog=args.catalog, bundle=None,
                                                     project=args.project)))
    return apply.run(host, only=args.only, dry_run=args.dry_run)


def cmd_build(args: argparse.Namespace) -> int:
    if int(args.all) + int(bool(args.services)) != 1:
        print("ordo build: give exactly one of --all or SERVICE...", file=sys.stderr)
        return 1
    return images.run_build(args.out, None if args.all else args.services, project=args.project,
                            dry_run=args.dry_run)
