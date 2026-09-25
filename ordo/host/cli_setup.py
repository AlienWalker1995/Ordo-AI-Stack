"""`ordo init` (alias `setup`) and `ordo remote enable|disable`: the install wizard and the opt-in
remote access. Argument parsing lives in ordo/cli.py.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from ..render import stack
from ..render.catalog import Catalog
from ..render.engine import DEFAULT_CATALOG, DEFAULT_PLUGINS_DIR
from ..render.plugins import PluginRegistry
from . import bringup, cli_render, cli_secrets, cli_stack, remote, secret_store, wizard

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], cwd: Path | None = None,
         env: dict[str, str] | None = None) -> int:  # pragma: no cover - shells out
    import os
    import subprocess
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    print(f"  $ {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=full_env).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  ! command failed: {e}")
        return 1


def _local_urls(compose_doc: dict) -> list[str]:
    """`http://127.0.0.1:<port>  (<service>)` for every UI the render publishes on loopback."""
    urls = []
    for name, spec in (compose_doc.get("services") or {}).items():
        for port in (spec or {}).get("ports") or []:
            parts = str(port).split(":")
            if len(parts) == 3 and parts[0] == "127.0.0.1":
                urls.append(f"http://127.0.0.1:{parts[1]}  ({name})")
    return urls


def cmd_init(args: argparse.Namespace) -> int:
    # --catalog may arrive via the global (before the subcommand) or the subparser (after it);
    # the subparser default is None, so fall back to the resolved global/bundled default.
    catalog_path = Path(args.catalog or DEFAULT_CATALOG)
    cat = Catalog.load(catalog_path)
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    out = Path(args.out)
    interactive = not args.yes and sys.stdin.isatty()

    # Live-tree guard: refuse to clobber an existing config unless --force. Protects the operator's
    # running out/ (ordo.yaml + secrets.env) from an accidental `ordo init` with the default --out.
    for existing in (out / "ordo.yaml", out / "secrets.env"):
        if existing.exists() and not args.force:
            print(f"refusing to overwrite existing {existing}: pass --out to a fresh directory "
                  f"or --force to replace it (this protects a running stack's config).")
            return 1

    try:
        result = wizard.run(cat, reg, out, interactive=interactive,
                            answers={} if not interactive else None, host_root=REPO_ROOT,
                            secrets_source=args.secrets_source)
    except wizard.SetupCancelled:
        # Operator aborted (Ctrl-C). Nothing was written: the files are written after the questions.
        print("\nSetup cancelled - nothing was written.")
        return 130
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"\nWrote {result.source_path}")
    print(f"Wrote {result.secrets_path}  (chmod 600)")
    if result.secrets_store and result.secrets_store != str(result.secrets_path):
        print(f"  secrets are kept in {result.secrets_store}; secrets.env is materialized from it")
    print(f"  Model   : {result.model_name} ({result.model_id})")
    print(f"  Plugins : {', '.join(result.plugins_enabled) or '(none)'}")
    print(f"  Tools   : {', '.join(result.mcp_servers) or '(none)'}")
    if result.generated_secret_keys:
        print(f"  generated {len(result.generated_secret_keys)} internal secret(s)")
    if result.optional_blank_secret_keys:
        print(f"  optional, blank until you need them: {', '.join(result.optional_blank_secret_keys)}")
    if result.blank_secret_keys:
        print(f"  ! required secret(s) left BLANK (set each before bring-up: ordo secrets set KEY --from-stdin): "
              f"{', '.join(result.blank_secret_keys)}")
    for w in result.warnings:
        print(f"  ! {w}")

    remote_line = f"Remote access (Tailscale + Google sign-in), any time later: ordo remote enable --out {out}"
    if not interactive:
        # Headless/CI: config only. NEVER render or bring up unattended (the safety line).
        print(f"\nNext (review first):\n  ordo render --source {result.source_path} --out {out}\n"
              f"  ordo up --all --out {out}\n{remote_line}")
        return 0

    # pragma: no cover below (interactive) - question 3 of 3.
    if not _prompt_yn("\n3/3  Start now? (render, check this host, download the model, bring the stack up)",
                      default=True):
        print(f"\nWhen ready:\n  ordo render --source {result.source_path} --out {out}\n"
              f"  ordo up --all --out {out}\n{remote_line}")
        return 0
    render_args = argparse.Namespace(source=str(result.source_path), source_explicit=True,
                                     catalog=str(catalog_path), out=str(out), force=False)
    if cli_render.cmd_render(render_args) != 0:
        return 1
    if not cli_stack._host_preflight(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                           catalog_path=str(catalog_path)):
        print(f"\nFix the above, then: ordo up --all --out {out}\n{remote_line}")
        return 1
    # build=True: the first up builds the stack's first-party images (`ordo build`); models_catalog
    # downloads the model files the stack loads into the models volume (`ordo fetch`).
    rc = bringup.bring_up(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                          force_recreate=False, dry_run=False, build=True, models_catalog=catalog_path)
    if rc != 0:
        return rc
    urls = _local_urls(stack.load_compose(out.resolve().as_posix()))
    if urls:
        print("\nOpen (this machine only):\n  " + "\n  ".join(urls))
    cli_secrets._print_dashboard_sign_in(out)
    print(remote_line)
    return 0


def _prompt_yn(msg: str, default: bool = True) -> bool:  # pragma: no cover - interactive only
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{msg} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # Ctrl-C during the post-setup offers: config is already written, so just decline the
        # remaining bring-up prompts rather than erroring out.
        print()
        return False
    if not ans:
        return default
    return ans in ("y", "yes")


def _remote_answers_from_flags(args: argparse.Namespace) -> wizard.RemoteAnswers:
    """--yes: every answer from flags; the OAuth pair may come from the environment instead, so the
    client secret need not sit in shell history."""
    return wizard.RemoteAnswers(
        hostname=args.hostname or "",
        bind=args.bind or "",
        client_id=args.client_id or os.environ.get("OAUTH2_PROXY_CLIENT_ID", ""),
        client_secret=args.client_secret or os.environ.get("OAUTH2_PROXY_CLIENT_SECRET", ""),
        emails=wizard.parse_emails(args.emails or ""),
    )


def _offer_tailscale_cert(hostname: str, interactive: bool) -> None:  # pragma: no cover - shells out
    argv = remote.tailscale_cert_argv(hostname)
    cert = remote.CERT_DIR / "tailnet.crt"
    if cert.exists():
        print(f"TLS cert present: {cert}")
        return
    if interactive and shutil.which("tailscale") and _prompt_yn("Issue the TLS cert now (tailscale cert)?"):
        remote.CERT_DIR.mkdir(parents=True, exist_ok=True)
        if _run(argv) == 0:
            return
    print(f"Issue the TLS cert (renew it every ~90 days):\n  {' '.join(argv)}")


def cmd_remote(args: argparse.Namespace) -> int:
    out = Path(args.out)
    source = Path(args.source) if args.source_explicit else out / "ordo.yaml"
    if not source.exists():
        print(f"no config at {source}: run `ordo init` first", file=sys.stderr)
        return 1
    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    interactive = not args.yes and sys.stdin.isatty()
    try:
        store = secret_store.store_for(cli_secrets._site_of(source), out, repo_root=REPO_ROOT)
        if args.action == "enable":
            answers = wizard.ask_remote_access() if interactive else _remote_answers_from_flags(args)
            change = remote.enable(source, store, answers, cat, reg)
        else:
            if interactive and not _prompt_yn("Turn remote access off (UIs go back to this machine only)?"):
                return 1
            change = remote.disable(source, store, cat, reg)
    except wizard.SetupCancelled:
        print("\ncancelled - nothing was written.")
        return 130
    except (ValueError, secret_store.SecretStoreError) as e:
        print(f"error: {e}\nnothing was written.", file=sys.stderr)
        return 1
    change.rendered.write(out)
    removed = remote.OAUTH_CLIENT_KEYS if args.action == "disable" else ()
    try:
        secret_store.materialize(store, secret_store.SecretNeeds.from_render(change.rendered), out,
                                 strict=False, removed=removed)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Updated {source} and {store.description}; rendered -> {out}/")
    if change.generated_secret_keys:
        print(f"  generated: {', '.join(change.generated_secret_keys)}")
    if change.blank_secret_keys:
        print(f"  ! still blank in {store.description}: {', '.join(change.blank_secret_keys)} "
              "(ordo secrets set KEY --from-stdin)")
    if args.action == "enable":
        print(f"Remote access on: https://{answers.hostname}/  "
              f"({len(answers.emails)} allowlisted address(es) in {remote.ALLOWLIST_PATH})")
        _offer_tailscale_cert(answers.hostname, interactive)
    else:
        print("Remote access off: the UIs publish on 127.0.0.1 again.")
    print(f"Apply it: ordo up --all --out {out}")
    return 0
