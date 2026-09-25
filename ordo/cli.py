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
    ordo secrets list|materialize|set|rotate|import   # the secret store (ordo/secret_store.py)

`render` writes to an output dir only (it starts nothing), and `serve`'s Docker backend is
hard-scoped to the ordo project prefix so it only ever touches its own project's containers.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

from . import (
    agents,
    apply,
    bringup,
    doctor,
    fetch,
    gpu,
    images,
    native,
    parity,
    preflight,
    remote,
    secret_store,
    served_models,
    wizard,
)
from .catalog import Catalog
from .config import SECRETS_BACKEND_KEY, Source
from .hardware import detect
from .plugins import PluginRegistry
from .render import DEFAULT_AGENTS_DIR, DEFAULT_PLUGINS_DIR, render
from .secret_env import SecretFileError, read_secret
from .source_edit import edit_site_keys

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


def _dashboard_sign_in(out: Path) -> dict | None:
    """The render's `dashboard_sign_in` record ({url, secret}); None with the edge on or no render."""
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8")).get("dashboard_sign_in")


def _ensure_local_sign_in_secret(out: Path, store: secret_store.Store | None = None) -> bool:
    """Mint the dashboard's local sign-in secret in the secret store when the render needs it and the
    store lacks it (a local install made before the secret existed). True when a value was written;
    an existing value is never replaced. Materializing it into secrets.env is the caller's step."""
    sign_in = _dashboard_sign_in(out)
    store = store if store is not None else secret_store.PlainStore(out / "secrets.env")
    if sign_in is None or not store.exists():
        return False
    if secret_store.parse_dotenv(store.read_text()).get(sign_in["secret"]):
        return False
    generated, _ = secret_store.update(store, [sign_in["secret"]])
    return sign_in["secret"] in generated


def _manifest(out: Path) -> dict | None:
    """The render's out/manifest.json, or None before the first render."""
    path = Path(out) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _source_path(args: argparse.Namespace) -> Path:
    """The operator source a host command edits: --source when given, else <out>/ordo.yaml."""
    return Path(args.source) if args.source_explicit else Path(args.out) / "ordo.yaml"


def _site_of(source_path: Path) -> dict:
    """The source's raw `site:` mapping. Read without validation, so `ordo secrets import` still runs
    on a source that carries a retired key it is about to remove."""
    if not source_path.exists():
        return {}
    site = (yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}).get("site") or {}
    return site if isinstance(site, dict) else {}


def _secret_store(args: argparse.Namespace) -> secret_store.Store:
    """The store the source names (`site: SECRETS_SOURCE`), or <out>/secrets.env without one."""
    return secret_store.store_for(_site_of(_source_path(args)), Path(args.out), repo_root=HERE)


def _prepare_secrets(args: argparse.Namespace, out: Path) -> int:
    """Before a bring-up: mint the local sign-in secret if the store lacks it, then materialize
    secrets.env and the file secrets from the store. Blank keys are left to the preflight."""
    manifest = _manifest(out)
    if manifest is None:
        return 0   # nothing rendered yet: bring_up reports that
    try:
        store = _secret_store(args)
        if _ensure_local_sign_in_secret(out, store):
            print(f"generated the dashboard's local sign-in secret in {store.description}")
        secret_store.materialize(store, secret_store.SecretNeeds.from_manifest(manifest), out, strict=False)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _dashboard_sign_in_link(out: Path) -> str | None:
    """`http://127.0.0.1:<port>/#sign-in=<token>` for the local operator; None with the edge on.

    The token rides in the URL fragment, which the browser never sends to the server (no access
    log line, no Referer); the dashboard's page posts it once and swaps it for a session cookie."""
    sign_in = _dashboard_sign_in(out)
    if sign_in is None or not (out / "secrets.env").exists():
        return None
    token = parity.load_env(str(out / "secrets.env")).get(sign_in["secret"], "")
    if not token:
        return None
    return f"{sign_in['url']}/#sign-in={token}"


def _print_dashboard_sign_in(out: Path) -> None:
    link = _dashboard_sign_in_link(out)
    if link:
        print(f"\nDashboard sign-in (this machine only; the link holds your sign-in token):\n  {link}")


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
                            answers={} if not interactive else None, host_root=HERE,
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
    if cmd_render(render_args) != 0:
        return 1
    if not _host_preflight(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                           catalog_path=str(catalog_path)):
        print(f"\nFix the above, then: ordo up --all --out {out}\n{remote_line}")
        return 1
    # build=True: the first up builds the stack's first-party images (`ordo build`); models_catalog
    # downloads the model files the stack loads into the models volume (`ordo fetch`).
    rc = bringup.bring_up(str(out), "ordo", [], whole_stack=True, with_profiles=True,
                          force_recreate=False, dry_run=False, build=True, models_catalog=catalog_path)
    if rc != 0:
        return rc
    urls = _local_urls(bringup.load_compose(out.resolve().as_posix()))
    if urls:
        print("\nOpen (this machine only):\n  " + "\n  ".join(urls))
    _print_dashboard_sign_in(out)
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
        store = secret_store.store_for(_site_of(source), out, repo_root=HERE)
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


# --------------------------------------------------------------------------- #
# `ordo secrets`: the one secret store (ordo/secret_store.py). Names only, never values.
# --------------------------------------------------------------------------- #


def _secret_needs(out: Path) -> secret_store.SecretNeeds | None:
    manifest = _manifest(out)
    if manifest is None:
        print(f"no render in {out}: run `ordo --source {out / 'ordo.yaml'} render --out {out}` first",
              file=sys.stderr)
        return None
    return secret_store.SecretNeeds.from_manifest(manifest)


def _materialize_after_edit(store: secret_store.Store, needs: secret_store.SecretNeeds, out: Path) -> int:
    """The materialize every store edit ends with. Blank required keys are reported, not fatal: the
    edit itself succeeded, and the preflight blocks a bring-up that needs them."""
    try:
        result = secret_store.materialize(store, needs, out, strict=False)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if store.materializes:
        print(f"materialized {result.secrets_env} and {len(result.files)} file secret(s) from {store.description}")
    if result.blank_required:
        print(f"  ! still blank (required): {', '.join(result.blank_required)}")
    return 0


def _print_recreate(keys: list[str], needs: secret_store.SecretNeeds, out: Path) -> None:
    """The command that applies changed keys: recreate their readers (a restart keeps the old env,
    and a file secret's bind mount keeps the replaced file). `--reading` finds env and file readers."""
    out_flag = "" if str(out) == "out" else f" --out {out}"
    print("Apply it, from the repo root, outside a GPU lease:")
    print(f"  ordo recreate --reading {' '.join(keys)}{out_flag}")


def _secrets_list(args: argparse.Namespace) -> int:
    store = _secret_store(args)
    backup_values: dict[str, str] | None = None
    if isinstance(store, secret_store.InfisicalStore):
        print(f"backend: infisical (site: SECRETS_BACKEND): {store.description}; "
              "out/secrets.env is materialized from it")
        if store.backup is None:
            print("  offline backup: none (set site: SECRETS_SOURCE to a SOPS file, then `ordo secrets backup`)")
        else:
            print(f"  offline backup: {store.backup.description} (`ordo secrets backup` refreshes it)")
            backup_values = secret_store.parse_dotenv(store.backup.read_text()) if store.backup.exists() else {}
    elif store.is_sops:
        print(f"backend: sops (site: SECRETS_SOURCE): {store.path}; out/secrets.env is materialized from it")
    else:
        print(f"backend: local file: {store.path} (no SECRETS_SOURCE configured: out/secrets.env is the store)")
    values = secret_store.parse_dotenv(store.read_text())   # a missing SOPS file raises: run import
    manifest = _manifest(Path(args.out))
    needs = secret_store.SecretNeeds.from_manifest(manifest) if manifest else secret_store.SecretNeeds(())
    if manifest is None:
        print(f"  (no render in {args.out}: every key shows as unused)")
    files: dict[str, list[secret_store.SecretFile]] = {}
    for secret_file in needs.files:
        files.setdefault(secret_file.key, []).append(secret_file)
    keys = list(dict.fromkeys([*needs.required, *files, *values]))
    width = max((len(k) for k in keys), default=0)
    for key in keys:
        state = "set" if values.get(key) else ("blank" if key in values else "absent")
        if key in needs.optional:
            role = "optional"
        elif key in needs.required:
            role = "required"
        elif key in files:
            role = "read as a file"
        else:
            role = "not read by this render"
        if key in files:
            readers = ", ".join(f.service for f in files[key])
            role += f"; file out/secrets/{files[key][0].file} ({readers})"
        line = f"  {key:<{width}}  {state:<6}  {role}"
        if backup_values is not None and values.get(key):
            line += "; " + secret_store.backup_status(values[key], backup_values.get(key, ""))
        print(line)
    return 0


def _secrets_materialize(args: argparse.Namespace) -> int:
    out = Path(args.out)
    needs = _secret_needs(out)
    if needs is None:
        return 1
    store = secret_store.SopsStore(Path(args.from_path).resolve()) if args.from_path else _secret_store(args)
    try:
        result = secret_store.materialize(store, needs, out)
    except secret_store.MissingSecrets as e:
        print(f"error: {e}\n  set each: ordo secrets set KEY --from-stdin (an internal one: --generate)",
              file=sys.stderr)
        return 1
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if store.materializes:
        print(f"materialized {len(needs.required)} key(s) into {result.secrets_env} from {store.description}")
    else:
        print(f"{result.secrets_env} is the store (no SECRETS_SOURCE configured): all required keys are set")
    print(f"  file secrets: {len(result.files)} in {out / 'secrets'}")
    if result.stale_files:
        print(f"  stale file secret(s) no render declares (kept: remove after recreating the services "
              f"that mounted them): {', '.join(p.name for p in result.stale_files)}")
    if result.blank_optional:
        print(f"  optional, blank: {', '.join(result.blank_optional)}")
    return 0


def _secrets_set(args: argparse.Namespace) -> int:
    out = Path(args.out)
    key = args.key
    needs = _secret_needs(out)
    if needs is None:
        return 1
    store = _secret_store(args)
    if key in secret_store.BOOTSTRAP_KEYS:
        # The Infisical identities' own credentials unlock the store, so they live in the SOPS file.
        bootstrap = secret_store.sops_file_of(store)
        if bootstrap is None:
            print(f"error: {key} is an Infisical bootstrap credential: it lives in the SOPS file "
                  "(site: SECRETS_SOURCE), and none is configured. Set it as an environment variable instead.",
                  file=sys.stderr)
            return 1
        store = bootstrap
    try:
        current = secret_store.parse_dotenv(store.read_text()).get(key, "")
        if args.generate:
            generator = secret_store.generator_for(key)
            if generator is None:
                print(f"error: {key} is issued by an outside service: use --from-stdin", file=sys.stderr)
                return 1
            refusal = secret_store.rotation_refusal(key) if current else None   # a new value is a rotation
            if refusal:
                print(f"error: {refusal}", file=sys.stderr)
                return 1
            value = generator()
        else:
            value = sys.stdin.read().strip()
            if not value:
                print(f"error: no value for {key} on stdin", file=sys.stderr)
                return 1
        secret_store.update(store, [], provided={key: value})
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"set {key} in {store.description}")
    if key in secret_store.BOOTSTRAP_KEYS:
        print(f"Commit {store.path.name} in its repo. Nothing to materialize: the stack does not read {key}.")
        return 0
    if _materialize_after_edit(store, needs, out) != 0:
        return 1
    _print_recreate([key], needs, out)
    _print_after_store_edit(store)
    return 0


def _secrets_rotate(args: argparse.Namespace) -> int:
    if bool(args.keys) == bool(args.internal):
        print("ordo secrets rotate: give exactly one of KEY... or --internal", file=sys.stderr)
        return 1
    out = Path(args.out)
    needs = _secret_needs(out)
    if needs is None:
        return 1
    store = _secret_store(args)
    try:
        keys = list(args.keys) or secret_store.internal_keys(store)
        if not keys:
            print(f"error: {store.description} holds no internal key to rotate", file=sys.stderr)
            return 1
        rotated = secret_store.rotate(store, keys)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}\nnothing was rotated.", file=sys.stderr)
        return 1
    print(f"rotated in {store.description}: {' '.join(rotated)}")
    if _materialize_after_edit(store, needs, out) != 0:
        return 1
    steps = [f"  {key}: {secret_store.ROTATION_STEPS[key]}" for key in rotated if key in secret_store.ROTATION_STEPS]
    if steps:
        print("Before recreating (the new value is in out/secrets.env; the stores keep their own copy):")
        print("\n".join(steps))
    _print_recreate(rotated, needs, out)
    for key in rotated:
        if key in secret_store.ROTATION_EFFECTS:
            print(f"  then: {secret_store.ROTATION_EFFECTS[key]}")
    _print_after_store_edit(store)
    return 0


def _print_after_store_edit(store: secret_store.Store) -> None:
    """What keeps the durable copy in step after an edit: commit the SOPS file, or back Infisical up."""
    if isinstance(store, secret_store.InfisicalStore):
        if store.backup is not None:
            print("Then refresh the offline copy: ordo secrets backup")
    elif store.is_sops:
        print(f"Then commit {store.path.name} in its repo.")


# The file names the retired OPERATOR_SECRETS_DIR used for the agent's file secrets. Only
# `ordo secrets import` reads that directory; everything else uses the key lowercased.
RETIRED_OPERATOR_SECRET_FILES = {"DISCORD_BOT_TOKEN": "discord_token", "GITHUB_BACKUP_PAT": "github_backup_pat"}


def _secrets_import(args: argparse.Namespace) -> int:
    out = Path(args.out)
    source = _source_path(args)
    site = _site_of(source)
    if str(site.get(SECRETS_BACKEND_KEY, "")).strip() == "infisical":
        print("error: the secret source is Infisical (site: SECRETS_BACKEND: infisical): `import` would write the "
              "SOPS file from out/secrets.env. Refresh the offline copy with `ordo secrets backup` instead.",
              file=sys.stderr)
        return 1
    live = Path(args.from_path) if args.from_path else out / "secrets.env"
    if args.to:
        target = Path(args.to).resolve()
    else:
        target = secret_store.resolve_source_path(
            str(site.get(secret_store.SECRETS_SOURCE_KEY) or secret_store.DEFAULT_SECRETS_SOURCE), HERE)
    files_dir = args.files_from or site.get("OPERATOR_SECRETS_DIR")
    agent_id = str((yaml.safe_load(source.read_text(encoding="utf-8")) or {}).get("agent", "hermes")
                   if source.exists() else "hermes")
    agent = agents.AgentRegistry.load(DEFAULT_AGENTS_DIR).get(agent_id)
    # The retired OPERATOR_SECRETS_DIR held the agent's file secrets under their V1 names.
    files = [secret_store.SecretFile(key=ref.key, file=RETIRED_OPERATOR_SECRET_FILES.get(ref.key, ref.file),
                                     service="agent")
             for ref in (agent.secret_files if agent else ())]
    if not live.exists() and not files_dir:
        print(f"error: nothing to import: {live} does not exist", file=sys.stderr)
        return 1
    store = secret_store.SopsStore(target)
    try:
        result = secret_store.import_values(store, live, files=files,
                                            files_dir=Path(files_dir) if files_dir else None,
                                            overwrite=args.overwrite)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"imported into {store.description}:")
    print(f"  added from {live}: {', '.join(result.added) or '(nothing new)'}")
    if files_dir:
        print(f"  added from {files_dir}: {', '.join(result.files_added) or '(nothing new)'}")
    if result.differing:
        verb = ("replaced with the live value" if args.overwrite
                else "kept the store's value (--overwrite takes the live one)")
        print(f"  different in the store, {verb}: {', '.join(result.differing)}")
    # Point the source at the store (the portable relative default when that is where it went), and
    # drop the retired OPERATOR_SECRETS_DIR: its files are in the store now.
    default = secret_store.resolve_source_path(secret_store.DEFAULT_SECRETS_SOURCE, HERE)
    wanted = secret_store.DEFAULT_SECRETS_SOURCE if target == default else target.as_posix()
    if not source.exists():
        print(f"  add to your source's site: block: SECRETS_SOURCE: {wanted}")
    else:
        configured = site.get(secret_store.SECRETS_SOURCE_KEY)
        already_set = bool(configured) and secret_store.resolve_source_path(str(configured), HERE) == target
        set_values = {} if already_set else {secret_store.SECRETS_SOURCE_KEY: wanted}
        remove = ["OPERATOR_SECRETS_DIR"] if "OPERATOR_SECRETS_DIR" in site else []
        if set_values or remove:
            try:
                source.write_text(edit_site_keys(source.read_text(encoding="utf-8"), set_values, remove),
                                  encoding="utf-8")
            except ValueError as e:
                print(f"error: could not edit {source}: {e}. Set site: SECRETS_SOURCE: {wanted} by hand",
                      file=sys.stderr)
                return 1
            changes = [f"removed {key}" for key in remove]
            if set_values:
                changes.insert(0, f"SECRETS_SOURCE: {wanted}")
            print(f"  {source}: {'; '.join(changes)}")
    print(f"Next: ordo --source {source} render --out {out}, then ordo secrets materialize --out {out}. "
          f"Commit {target.name} in its repo.")
    return 0


def _secrets_backup(args: argparse.Namespace) -> int:
    store = _secret_store(args)
    if not isinstance(store, secret_store.InfisicalStore):
        print("error: `backup` copies an Infisical project into the SOPS file: it needs site: "
              "SECRETS_BACKEND: infisical (the active store is the SOPS file or out/secrets.env itself)",
              file=sys.stderr)
        return 1
    if store.backup is None:
        print("error: no offline copy configured: set site: SECRETS_SOURCE to the SOPS file to back up into",
              file=sys.stderr)
        return 1
    result = secret_store.backup(store, store.backup)
    print(f"backup of {store.label} into {store.backup.description}:")
    if not (result.added or result.updated):
        print("  the SOPS file already matches: nothing written")
    if result.added:
        print(f"  added: {', '.join(result.added)}")
    if result.updated:
        print(f"  updated: {', '.join(result.updated)}")
    if result.only_in_backup:
        print(f"  only in the SOPS file (kept): {', '.join(result.only_in_backup)}")
    if result.added or result.updated:
        print(f"Commit {store.backup.path.name} in its repo.")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    handlers = {"list": _secrets_list, "materialize": _secrets_materialize, "set": _secrets_set,
                "rotate": _secrets_rotate, "import": _secrets_import, "backup": _secrets_backup}
    try:
        return handlers[args.action](args)
    except secret_store.SecretStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


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
    present = None if args.no_images else _local_images()
    image_tags = images.load_record(args.out)
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
        doc = bringup.load_compose(out.resolve().as_posix())
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
    runner = fetch.DockerRunner()
    volume = fetch.volume_name(args.project)
    exists = fetch.volume_exists(runner, volume)
    present = (fetch.files_in_volume(runner, volume) or set()) if exists else set()
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


def cmd_native(args: argparse.Namespace) -> int:
    src, cat = _load(Path(args.source), Path(args.catalog))
    rc = render(src, cat)
    print(native.plan(rc, models_dir=args.models_dir).as_text())
    return 0


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
        doc = bringup.load_compose(out.resolve().as_posix())
    except (OSError, ValueError):
        return True
    if any(name not in (doc.get("services") or {}) for name in services):
        return True
    starting = bringup.starting_services(doc, services, whole_stack=whole_stack, with_profiles=with_profiles)
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
    if not args.dry_run and _prepare_secrets(args, out) != 0:
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
        _print_dashboard_sign_in(out)
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
            doc = bringup.load_compose(Path(args.out).resolve().as_posix())
        except (OSError, yaml.YAMLError) as e:
            print(f"cannot read {args.out}/{bringup.COMPOSE_FILE} ({e}); render first: "
                  "ordo --source out/ordo.yaml render --out out", file=sys.stderr)
            return 1
        services = bringup.readers_of(doc, args.reading)
        if not services:
            print(f"no rendered service reads {', '.join(args.reading)}; nothing to recreate")
            return 0
        print(f"services reading {', '.join(args.reading)}: {' '.join(services)}")
    return bringup.bring_up(args.out, args.project, services, whole_stack=False,
                            with_profiles=True, force_recreate=True, dry_run=args.dry_run,
                            build=not args.no_build)


def cmd_apply(args: argparse.Namespace) -> int:
    """`ordo apply`: the deploy, in its one correct order (ordo/apply.py)."""
    out = Path(args.out)
    source = _source_path(args)
    if not source.exists():
        print(f"no operator source at {source}: run `ordo init`, or pass --source", file=sys.stderr)
        return 1
    host = apply.RealHost(
        source_path=source, catalog_path=Path(args.catalog), out=out, project=args.project,
        preflight=lambda services: _host_preflight(args.out, args.project, services, whole_stack=False,
                                                   with_profiles=True, catalog_path=args.catalog),
        materialize_secrets=lambda: _prepare_secrets(args, out),
        doctor=lambda: cmd_doctor(argparse.Namespace(source=str(source), catalog=args.catalog, bundle=None,
                                                     project=args.project)))
    return apply.run(host, only=args.only, dry_run=args.dry_run)


def cmd_build(args: argparse.Namespace) -> int:
    if int(args.all) + int(bool(args.services)) != 1:
        print("ordo build: give exactly one of --all or SERVICE...", file=sys.stderr)
        return 1
    return images.run_build(args.out, None if args.all else args.services, project=args.project,
                            dry_run=args.dry_run)


def cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - binds a socket
    import threading
    import time

    # The control plane's libraries (fastapi, uvicorn, pydantic, httpx) are the `serve` extra:
    # imported here so every other command runs on the PyYAML-only core.
    from .broker import Broker, DockerBackend
    from .control import ControlPlane
    from .scheduler import Scheduler

    cat = Catalog.load(Path(args.catalog))
    reg = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    src = Source.load(Path(args.source))
    hw = detect()
    sched = Scheduler(hw.primary_vram_gb if hw.has_gpu else 0.0)
    # Durable lease record (served at GET /jobs/history for the dashboard's orchestration tab).
    # Lives next to the rendered outputs — the same writable /config mount, no extra volume.
    from .lease_history import LeaseHistory

    history = LeaseHistory(Path(args.out) / "lease-history.jsonl")
    # The scheduler's live lease and eviction state, on the /data bind (SCHEDULER_STATE_PATH, set
    # by the rendered compose). Unset (a hand-run `ordo serve`) means it is kept in memory only.
    from .scheduler_state import SchedulerStateStore

    state_path = os.environ.get("SCHEDULER_STATE_PATH", "").strip()
    state_store = SchedulerStateStore(Path(state_path)) if state_path else None
    broker = Broker(sched, DockerBackend(project=args.project), history=history, state_store=state_store)
    cp = ControlPlane(Path(args.source), cat, reg, args.out, scheduler=sched, broker=broker,
                      history=history,
                      model_volume_files=lambda: fetch.volume_files(fetch.DockerRunner(), args.project))

    # Resident registration, DERIVED from the declared GPU inventory (ordo/gpu.py) rather than
    # from a `--resident-service llamacpp` default. Every service that DECLARES it holds VRAM on
    # the primary device and may be reclaimed is registered as idle-cached, so a burst request
    # can actually evict it; an unregistered resident's VRAM looks free and the scheduler admits
    # a job into space that is already taken (the live defect: /status showed free == total).
    # Secondary-device residents (the 1070's voice models) are excluded on purpose, and a
    # non-preemptible resident's VRAM is removed from the budget instead of being offered.
    # Read from the same render the stack runs, so it can't drift from what `.env` loads.
    if hw.has_gpu:
        rc = render(src, cat, reg)
        claims = rc.gpu_inventory()
        pinned = gpu.pinned_primary_vram_gb(claims)
        if pinned:
            sched.total_vram_gb = round(sched.total_vram_gb - pinned, 2)
            print(f"[scheduler] {pinned:.1f}GB of the primary card is held by non-preemptible "
                  f"residents — removed from the admission budget", flush=True)
        for service, vram in gpu.primary_residents(claims).items():
            sched.cache_idle(service, vram)
            print(f"[scheduler] resident '{service}' ~{vram:.1f}GB registered as reclaimable",
                  flush=True)
        for c in claims:
            degraded = f" -> {c.degraded_service}" if c.degraded_service else ""
            print(f"[scheduler] gpu claim: {c.service:<16} mode={c.mode:<8} "
                  f"enforcement={c.enforcement:<7} device={c.device:<9} "
                  f"vram={c.vram_gb:>6.1f}GB yield={c.yield_strategy}{degraded}", flush=True)

    # Adopt the previous process's lease state BEFORE the lease loop or the API run: a lease held
    # across this restart keeps its resident evicted, and one that expired while down is swept.
    if state_store is not None:
        broker.restore_state()
        print(f"[scheduler] lease state persisted at {state_path}; adopted "
              f"running={sched.running_ids} queued={sched.queued_ids} "
              f"evicted={sorted(sched.evicted_residents)}", flush=True)
    else:
        print("[scheduler] SCHEDULER_STATE_PATH is not set: the lease state is in memory only, and "
              "a restart mid-lease loses it", flush=True)

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
                stray = broker.enforce_evictions()
                if stray:
                    print(f"[scheduler] ERROR: evicted resident(s) {stray} were running during a GPU "
                          f"lease (started outside the scheduler, e.g. a whole-stack compose up); "
                          f"stopped them again", flush=True)
            except Exception as e:  # noqa: BLE001 — the control plane must survive a sweep hiccup
                print(f"[scheduler] lease sweep error: {e}", flush=True)

    threading.Thread(target=_lease_loop, daemon=True, name="lease-sweep").start()

    try:
        # A file under /run/secrets (OPS_CONTROLLER_TOKEN_FILE, the rendered delivery), else the env var.
        token = read_secret("OPS_CONTROLLER_TOKEN")
    except SecretFileError as e:
        print(f"ops-controller: {e}; refusing to serve", file=sys.stderr, flush=True)
        return 2
    if not token:
        print("ops-controller: OPS_CONTROLLER_TOKEN is not set; refusing to serve an unauthenticated "
              "control plane (it is provisioned in the secret store)", file=sys.stderr, flush=True)
        return 2
    print(f"ops-controller on {args.host}:{args.port} (project={args.project}, "
          f"{sched.total_vram_gb:.0f}GB GPU) — Ctrl-C to stop")
    cp.serve(token, host=args.host, port=args.port)
    return 0


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
    sub.add_parser("detect").set_defaults(func=cmd_detect)
    pr = sub.add_parser("render")
    pr.add_argument("--out", default="out")
    pr.add_argument("--force", action="store_true",
                    help="render the default/example source even if --out holds a differing ordo.yaml "
                         "(overrides the anti-clobber guard)")
    pr.set_defaults(func=cmd_render)
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
                             f"{secret_store.DEFAULT_SECRETS_SOURCE}); default: out/secrets.env is the store")
        pi.set_defaults(func=cmd_init)
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
    prm.set_defaults(func=cmd_remote)
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
                                  f"{secret_store.DEFAULT_SECRETS_SOURCE} beside the checkout)")
    psi.add_argument("--files-from", help="directory holding the agent's file secrets "
                                          "(default: the retired site OPERATOR_SECRETS_DIR, when set)")
    psi.add_argument("--overwrite", action="store_true",
                     help="take the live value for a key the store holds with a different one")
    psb = psec_sub.add_parser("backup", help="copy the Infisical project into the SOPS file (the offline copy)")
    for sp in (psl, psm, pss, psr, psi, psb):
        sp.add_argument("--out", default="out", help="the config + rendered stack directory (default: out)")
        sp.set_defaults(func=cmd_secrets)
    pp = sub.add_parser("parity")
    pp.add_argument("--ref", required=True, help="reference .env to compare the render against")
    pp.set_defaults(func=cmd_parity)
    pd = sub.add_parser("doctor")
    pd.add_argument("--bundle", help="write a sanitized support bundle to this path")
    pd.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pd.set_defaults(func=cmd_doctor)
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
    pget.set_defaults(func=cmd_fetch)
    pn = sub.add_parser("native")
    pn.add_argument("--models-dir", default="./models", help="where the GGUF files live natively")
    pn.set_defaults(func=cmd_native)
    pf = sub.add_parser("preflight")
    pf.add_argument("--ref", help="live .env to parity-check against (merge gate)")
    pf.add_argument("--secrets", help="local secrets.env to check required keys against (non-blocking)")
    pf.add_argument("--project", default="ordo")
    pf.add_argument("--no-images", action="store_true", help="skip the docker image-presence check")
    pf.add_argument("--out", default="out",
                    help="the rendered stack directory whose images.json build record to check (default: out)")
    pf.add_argument("--no-host", action="store_true",
                    help="skip the host checks (docker daemon, compose v2, NVIDIA runtime, disk, ports)")
    pf.set_defaults(func=cmd_preflight)
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
    for sp, func in ((pu, cmd_up), (prc, cmd_recreate)):
        sp.add_argument("--out", default="out", help="the rendered stack directory (default: out)")
        sp.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
        sp.add_argument("--dry-run", action="store_true",
                        help="check the lease and print the docker compose argv without running it")
        sp.add_argument("--no-build", action="store_true",
                        help="do not build first-party images the rendered compose names but the "
                             "daemon lacks (compose then fails on the missing image)")
        sp.set_defaults(func=func)
    # `build`: first-party images, tagged with the commit that last changed each image's inputs and
    # recorded in <out>/images.json, which every render pins the compose to (ordo/images.py).
    pb = sub.add_parser("build", help="build the first-party images the rendered stack runs")
    pb.add_argument("services", nargs="*", metavar="SERVICE", help="build only these services' images")
    pb.add_argument("--all", action="store_true", help="every first-party image the rendered compose names")
    pb.add_argument("--out", default="out", help="the rendered stack directory (default: out)")
    pb.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pb.add_argument("--dry-run", action="store_true", help="print what would be built, build nothing")
    pb.set_defaults(func=cmd_build)
    # `apply`: the deploy. Builds what changed, renders, materializes secrets, then recreates
    # exactly the services whose config or image changed, ops-controller first (ordo/apply.py).
    pa = sub.add_parser("apply", help="deploy the checkout and the source: recreate exactly what changed")
    pa.add_argument("--out", default="out", help="the config + rendered stack directory (default: out)")
    pa.add_argument("--project", default="ordo", help="compose project name (default: ordo)")
    pa.add_argument("--dry-run", action="store_true",
                    help="print the plan (builds, changed services and why, lease state); change nothing")
    pa.add_argument("--only", nargs="+", metavar="SERVICE", default=None,
                    help="recreate only these of the changed services (a changed ops-controller still goes first)")
    pa.set_defaults(func=cmd_apply)
    pv = sub.add_parser("serve")
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=9000)
    pv.add_argument("--out", default="out")
    pv.add_argument("--project", default="ordo", help="container project prefix the broker may touch")
    pv.add_argument("--lease-poll-seconds", type=float, default=10.0,
                    help="how often the scheduler advances its lease clock + sweeps expired leases")
    pv.set_defaults(func=cmd_serve)
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
