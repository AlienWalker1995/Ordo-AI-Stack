"""ordo CLI — the seed of the one-script.

    ordo detect                 # show detected hardware + what it would pick
    ordo render [--out DIR]     # render config from ordo.yaml into DIR (default ./out)
    ordo doctor                 # sanity checks (catalog integrity, source validity)
    ordo serve                  # run the control-plane HTTP service (ops-controller)
    ordo build [--all|SVC…]     # build the first-party images the rendered stack runs, tagged by commit
    ordo fetch [MODEL]          # download model files into the models volume, checksum-verified
    ordo up [--all|--core|SVC…] # build missing images, fetch missing models, bring the stack up (GPU-lease checked)
    ordo recreate SVC…          # force-recreate services from the host (GPU-lease checked)
    ordo apply [--dry-run]      # the deploy: build, render, secrets, then recreate exactly what changed
    ordo secrets list|materialize|set|rotate|import   # the secret store (ordo/host/secret_store.py)

`render` writes to an output dir only (it starts nothing), and `serve`'s Docker backend is
hard-scoped to the ordo project prefix so it only ever touches its own project's containers.

This module only parses arguments and dispatches. The handlers live with their layer: the host
commands in ordo/host/cli_*.py, `serve` in ordo/control/serve.py. A handler's module is imported
only when its command runs, so the control-plane process (`ordo serve`, ops-controller's
entrypoint) never loads the host tooling. The layers and their import rule: ordo/__init__.py.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable

from .render.config import DEFAULT_SECRETS_SOURCE
from .render.engine import DEFAULT_CATALOG, DEFAULT_SOURCE


def _handler(module: str, name: str) -> Callable[[argparse.Namespace], int]:
    """The handler `module.name`, imported when its command runs rather than when the parser is built."""
    def run(args: argparse.Namespace) -> int:
        return getattr(importlib.import_module(module), name)(args)
    return run


def main(argv: list[str] | None = None) -> int:
    # Never let a stray non-ASCII byte crash the wizard on a legacy console (e.g. Windows cp1252):
    # degrade unencodable chars to a placeholder instead of raising UnicodeEncodeError mid-prompt.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # not a reconfigurable TextIOWrapper (redirected/wrapped) — fine, leave as-is
    p = argparse.ArgumentParser(prog="ordo", description="Ordo config render engine")
    # Default is a SENTINEL (None), resolved to DEFAULT_SOURCE below, so cmd_render can tell an
    # explicit `--source ordo.example.yaml` apart from the implicit default (see _guard_render_source).
    p.add_argument("--source", default=None)
    p.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect").set_defaults(func=_handler("ordo.host.cli_render", "cmd_detect"))
    pr = sub.add_parser("render")
    pr.add_argument("--out", default="out")
    pr.add_argument("--force", action="store_true",
                    help="render the default/example source even if --out holds a differing ordo.yaml "
                         "(overrides the anti-clobber guard)")
    pr.set_defaults(func=_handler("ordo.host.cli_render", "cmd_render"))
    # `init` = the one-command install wizard: at most three questions (model, features, start
    # now), local-only, no accounts. `setup` is a backwards-compatible alias. Both write a
    # DIRECTORY (--out) holding the config the stack runs. Remote access is `ordo remote enable`.
    for name in ("init", "setup"):
        pi = sub.add_parser(name)
        pi.add_argument("--out", default="out",
                        help="directory for ordo.yaml + secrets.env (default: out)")
        pi.add_argument("--yes", action="store_true",
                        help="non-interactive: write config only, never render/bring-up")
        pi.add_argument("--force", action="store_true",
                        help="overwrite existing ordo.yaml/secrets.env in --out (clobbers a running "
                             "stack's config)")
        # Accept --catalog after the subcommand too (the global one must precede it); a value here
        # overrides the global default so `ordo init --catalog X` works as written.
        pi.add_argument("--catalog", default=None,
                        help="model catalog to size against (defaults to the bundled catalog)")
        pi.add_argument("--secrets-source", default=None,
                        help="keep the secrets in this SOPS file (site SECRETS_SOURCE, e.g. "
                             f"{DEFAULT_SECRETS_SOURCE}); default: out/secrets.env is the store")
        pi.set_defaults(func=_handler("ordo.host.cli_setup", "cmd_init"))
    # `remote enable|disable`: the opt-in remote access (Tailscale HTTPS + Google SSO edge).
    prm = sub.add_parser("remote", help="turn remote access (Tailscale + Google sign-in) on or off")
    prm.add_argument("action", choices=["enable", "disable"])
    prm.add_argument("--out", default="out", help="the config + rendered stack directory (default: out)")
    prm.add_argument("--yes", action="store_true", help="no prompts: take every answer from the flags below")
    prm.add_argument("--hostname", help="tailnet hostname, e.g. ordo.tail1234.ts.net")
    prm.add_argument("--bind", help="Caddy bind address: the tailnet IP, or 0.0.0.0")
    prm.add_argument("--client-id", help="Google OAuth client id (or env OAUTH2_PROXY_CLIENT_ID)")
    prm.add_argument("--client-secret",
                     help="Google OAuth client secret (prefer env OAUTH2_PROXY_CLIENT_SECRET: flags land in history)")
    prm.add_argument("--emails", help="allowlisted Google accounts, comma-separated")
    prm.set_defaults(func=_handler("ordo.host.cli_setup", "cmd_remote"))
    # `secrets`: the secret store. With `site: SECRETS_BACKEND: infisical` it is an Infisical project,
    # with `site: SECRETS_SOURCE` alone a SOPS file (a private repo); out/secrets.env is materialized from
    # either. Without both, out/secrets.env is the store.
    psec = sub.add_parser("secrets",
                          help="list, materialize, set, rotate, import or back up the operator's secrets")
    psec_sub = psec.add_subparsers(dest="action", required=True)
    psl = psec_sub.add_parser("list", help="key names, whether each is set, and what the render needs")
    psm = psec_sub.add_parser("materialize", help="write out/secrets.env and out/secrets/* from the store")
    psm.add_argument("--from", dest="from_path", help="the SOPS file to read (default: site SECRETS_SOURCE)")
    pss = psec_sub.add_parser("set", help="set one key in the store, then materialize")
    pss.add_argument("key", metavar="KEY")
    how = pss.add_mutually_exclusive_group(required=True)
    how.add_argument("--from-stdin", action="store_true", help="read the value from stdin (never from argv)")
    how.add_argument("--generate", action="store_true", help="mint a value (internal secrets only)")
    psr = psec_sub.add_parser("rotate", help="give keys fresh generated values, then materialize")
    psr.add_argument("keys", nargs="*", metavar="KEY")
    psr.add_argument("--internal", action="store_true",
                     help="every internal token the store holds (salts and issued keys excluded)")
    psi = psec_sub.add_parser("import", help="one-time: add every value the store lacks from the live secrets.env")
    psi.add_argument("--from", dest="from_path", help="the live file to read (default: <out>/secrets.env)")
    psi.add_argument("--to", help="the SOPS file to create or extend (default: site SECRETS_SOURCE, else "
                                  f"{DEFAULT_SECRETS_SOURCE} beside the checkout)")
    psi.add_argument("--files-from", help="directory holding the agent's file secrets "
                                          "(default: the retired site OPERATOR_SECRETS_DIR, when set)")
    psi.add_argument("--overwrite", action="store_true",
                     help="take the live value for a key the store holds with a different one")
    psb = psec_sub.add_parser("backup", help="copy the Infisical project into the SOPS file (the offline copy)")
    for sp in (psl, psm, pss, psr, psi, psb):
        sp.add_argument("--out", default="out", help="the config + rendered stack directory (default: out)")
        sp.set_defaults(func=_handler("ordo.host.cli_secrets", "cmd_secrets"))
    pp = sub.add_parser("parity")
    pp.add_argument("--ref", required=True, help="reference .env to compare the render against")
    pp.set_defaults(func=_handler("ordo.host.cli_render", "cmd_parity"))
    pd = sub.add_parser("doctor")
    pd.add_argument("--bundle", help="write a sanitized support bundle to this path")
    pd.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pd.set_defaults(func=_handler("ordo.host.cli_render", "cmd_doctor"))
    pget = sub.add_parser("fetch", help="download catalog models into the models volume, checksum-verified")
    pget.add_argument("model", nargs="?",
                      help="catalog id (default: every model file the rendered stack in --out loads)")
    pget.add_argument("--all", action="store_true", help="fetch every catalog entry")
    pget.add_argument("--out", default="out", help="the rendered stack directory (default: out)")
    pget.add_argument("--project", default="ordo",
                      help="compose project whose <project>_models-gguf volume receives the files (default: ordo)")
    pget.add_argument("--models-dir", default=None,
                      help="download to this host directory instead of the volume (the native, non-Docker path)")
    pget.add_argument("--allow-unverified", action="store_true",
                      help="permit downloading a model with no pinned sha256 (unsafe)")
    pget.add_argument("--plan-only", action="store_true", help="print the plan, download nothing")
    pget.set_defaults(func=_handler("ordo.host.cli_stack", "cmd_fetch"))
    pn = sub.add_parser("native")
    pn.add_argument("--models-dir", default="./models", help="where the GGUF files live natively")
    pn.set_defaults(func=_handler("ordo.host.cli_render", "cmd_native"))
    pf = sub.add_parser("preflight")
    pf.add_argument("--ref", help="live .env to parity-check against (merge gate)")
    pf.add_argument("--secrets", help="local secrets.env to check required keys against (non-blocking)")
    pf.add_argument("--project", default="ordo")
    pf.add_argument("--no-images", action="store_true", help="skip the docker image-presence check")
    pf.add_argument("--out", default="out",
                    help="the rendered stack directory whose images.json build record to check (default: out)")
    pf.add_argument("--no-host", action="store_true",
                    help="skip the host checks (docker daemon, compose v2, NVIDIA runtime, disk, ports)")
    pf.set_defaults(func=_handler("ordo.host.cli_stack", "cmd_preflight"))
    # `up` / `recreate`: the one host bring-up path. Both env files and every rendered profile
    # (the argv builder is shared with ops-controller), named services never cascade onto their
    # dependencies (caddy excepted: it takes its netns members), and both refuse when the GPU
    # lease would be violated.
    pu = sub.add_parser("up", help="bring the rendered stack up (refuses during a GPU lease)")
    pu.add_argument("services", nargs="*", metavar="SERVICE",
                    help="start only these services (--no-deps; caddy also takes its netns members)")
    pu.add_argument("--all", action="store_true", help="whole stack, every rendered profile")
    pu.add_argument("--core", action="store_true", help="whole stack without profiles: core + agent")
    pu.add_argument("--no-preflight", action="store_true",
                    help="skip the host checks (docker, GPU runtime, disk, ports, secrets) run before starting")
    pu.add_argument("--no-fetch", action="store_true",
                    help="do not download the model files the starting services need into the models "
                         "volume (a service whose file is missing then fails to load it)")
    prc = sub.add_parser("recreate", help="force-recreate services (refuses an evicted GPU resident)")
    prc.add_argument("services", nargs="*", metavar="SERVICE")
    prc.add_argument("--reading", nargs="+", metavar="KEY", default=[],
                     help="instead of naming services, recreate every long-running service whose rendered "
                          "definition reads one of these keys (after rotating secrets)")
    for sp, func in ((pu, _handler("ordo.host.cli_stack", "cmd_up")),
                     (prc, _handler("ordo.host.cli_stack", "cmd_recreate"))):
        sp.add_argument("--out", default="out", help="the rendered stack directory (default: out)")
        sp.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
        sp.add_argument("--dry-run", action="store_true",
                        help="check the lease and print the docker compose argv without running it")
        sp.add_argument("--no-build", action="store_true",
                        help="do not build first-party images the rendered compose names but the "
                             "daemon lacks (compose then fails on the missing image)")
        sp.set_defaults(func=func)
    # `build`: first-party images, tagged with the commit that last changed each image's inputs and
    # recorded in <out>/images.json, which every render pins the compose to (ordo/render/image_tags.py).
    pb = sub.add_parser("build", help="build the first-party images the rendered stack runs")
    pb.add_argument("services", nargs="*", metavar="SERVICE", help="build only these services' images")
    pb.add_argument("--all", action="store_true", help="every first-party image the rendered compose names")
    pb.add_argument("--out", default="out", help="the rendered stack directory (default: out)")
    pb.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pb.add_argument("--dry-run", action="store_true", help="print what would be built, build nothing")
    pb.set_defaults(func=_handler("ordo.host.cli_stack", "cmd_build"))
    # `apply`: the deploy. Builds what changed, renders, materializes secrets, then recreates
    # exactly the services whose config or image changed, ops-controller first (ordo/host/apply.py).
    pa = sub.add_parser("apply", help="deploy the checkout and the source: recreate exactly what changed")
    pa.add_argument("--out", default="out", help="the config + rendered stack directory (default: out)")
    pa.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pa.add_argument("--dry-run", action="store_true",
                    help="print the plan (builds, changed services and why, lease state); change nothing")
    pa.add_argument("--only", nargs="+", metavar="SERVICE", default=None,
                    help="recreate only these of the changed services (a changed ops-controller still goes first)")
    pa.set_defaults(func=_handler("ordo.host.cli_stack", "cmd_apply"))
    pv = sub.add_parser("serve")
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=9000)
    pv.add_argument("--out", default="out")
    pv.add_argument("--project", default="ordo", help="container project prefix the broker may touch")
    pv.add_argument("--lease-poll-seconds", type=float, default=10.0,
                    help="how often the scheduler advances its lease clock + sweeps expired leases")
    pv.set_defaults(func=_handler("ordo.control.serve", "cmd_serve"))
    # Accept --source after the subcommand too (`ordo render --source X`, the form `ordo init`
    # prints). SUPPRESS leaves a global `ordo --source X render` untouched when it is absent here.
    for subparser in [*sub.choices.values(), *psec_sub.choices.values()]:
        if "--source" not in subparser._option_string_actions:
            subparser.add_argument("--source", default=argparse.SUPPRESS,
                                   help="the operator source (same as the global --source)")
    args = p.parse_args(argv)
    # Distinguish an explicit `--source` from the implicit default, then resolve the sentinel so
    # every command still sees a concrete path (unchanged behaviour for all but cmd_render's guard).
    args.source_explicit = args.source is not None
    if args.source is None:
        args.source = str(DEFAULT_SOURCE)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
