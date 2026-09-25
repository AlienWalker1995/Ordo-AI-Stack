"""`ordo secrets`: the one secret store (ordo/host/secret_store.py). Names only, never values.

Also the store steps other host commands share: materializing secrets before a bring-up, and the
dashboard's local sign-in secret and link. Argument parsing lives in ordo/cli.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ..render import agents
from ..render.config import DEFAULT_SECRETS_SOURCE, SECRETS_BACKEND_KEY
from ..render.engine import DEFAULT_AGENTS_DIR
from ..render.source_edit import edit_site_keys
from . import parity, secret_store

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    return secret_store.store_for(_site_of(_source_path(args)), Path(args.out), repo_root=REPO_ROOT)


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
            str(site.get(secret_store.SECRETS_SOURCE_KEY) or DEFAULT_SECRETS_SOURCE), REPO_ROOT)
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
    default = secret_store.resolve_source_path(DEFAULT_SECRETS_SOURCE, REPO_ROOT)
    wanted = DEFAULT_SECRETS_SOURCE if target == default else target.as_posix()
    if not source.exists():
        print(f"  add to your source's site: block: SECRETS_SOURCE: {wanted}")
    else:
        configured = site.get(secret_store.SECRETS_SOURCE_KEY)
        already_set = bool(configured) and secret_store.resolve_source_path(str(configured), REPO_ROOT) == target
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
