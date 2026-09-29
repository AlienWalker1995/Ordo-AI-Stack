"""`ordo backup` / `ordo restore`: the stack's state, saved to one archive and put back.

The archive holds the operator's config in the rendered stack directory (`out/ordo.yaml`,
`out/secrets.env`, `out/images.json`) and the project's named volumes. It does not hold host bind
mounts: the `DATA_PATH` directories (`data/`: audit log, dashboard samples, render outputs, the RAG
drop zone) and the memory vault (`MEMORY_VAULT_PATH`) are plain host files for the host's own
backup. How each volume is saved is declared by the manifest that owns it
(ordo/render/backup_policy.py) and read from the render's `out/manifest.json`, so this module holds
no list of volumes.

A restore never destroys what it is replacing before the replacement is proven: a file snapshot is
unpacked into a staging directory inside the volume and swapped in by renames (rolled back if a
rename fails), and a dump is loaded into a temporary database that is renamed over the old one in
one transaction. A restore that fails leaves the services it stopped stopped, and says what to do.

The archive is an uncompressed tar holding `manifest.json` (what it holds, the method of each
volume, the images and image ids of the services that wrote it, a sha256 per member), `config/*`,
`volumes/<volume>.tar.gz` (a file snapshot) and `databases/<volume>.pgdump` (a `pg_dump -Fc`).
It contains secrets: it is written outside the checkout, readable by the operator only.

Services are stopped and started with `docker compose stop|start` (the same argv builder every host
command uses, ordo/render/stack.py), never recreated, and a start goes through the GPU lease check
of `ordo up` (ordo/host/bringup.py). A restore refuses while any GPU lease is held.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from ..render import backup_policy, stack
from ..render.changed_set import OPS_CONTROLLER_SERVICE
from ..render.models_volume import HELPER_IMAGE
from . import bringup, parity

ARCHIVE_FORMAT = 1
MANIFEST_MEMBER = "manifest.json"
# The operator's config in the rendered stack directory: the source, the secrets materialized from
# the store, and the image record every render pins the compose to.
CONFIG_FILES = ("ordo.yaml", "secrets.env", "images.json")
# The files that hold secret values: written back readable by the operator only.
SECRET_CONFIG_FILES = frozenset({"secrets.env"})
DEFAULT_BACKUP_DIR = Path.home() / "ordo-backups"
# The checkout this module runs from (…/ordo/host/backup.py -> the repo root). No archive is
# written inside it: it would sit one `git add` away from a public repository.
REPO_ROOT = Path(__file__).resolve().parents[2]
# The file-snapshot helper: busybox tar + gzip in the digest-pinned image the models volume already
# uses (ordo/render/models_volume.py). It runs as root so it can read and restore any owner's files.
VOLUME_MOUNT = "/volume"
# A restore unpacks into SWAP_STAGING_DIR inside the volume, moves the current entries into
# SWAP_PREVIOUS_DIR, moves the staged entries up, then deletes the previous ones. Every step before
# the last is a rename on one filesystem, so nothing is deleted until the new contents are in place.
# The volume root itself cannot be renamed, so it takes the snapshot root's owner and mode instead.
SWAP_STAGING_DIR = ".ordo-restore-staging"
SWAP_PREVIOUS_DIR = ".ordo-restore-previous"
SWAP_LEFTOVER = 3        # a crashed restore left SWAP_PREVIOUS_DIR: refused, the volume is unchanged
SWAP_UNCHANGED = 4       # the unpack or a rename failed; the volume holds its previous contents
SWAP_ROLLBACK_FAILED = 5  # a rename failed and so did the rollback: both copies are in the volume
# POSIX sh (busybox in the helper image); $1 is the volume's mount point.
REPLACE_VOLUME_SCRIPT = f"""
V="$1"; S="$V/{SWAP_STAGING_DIR}"; P="$V/{SWAP_PREVIOUS_DIR}"
move_all() {{
  for e in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    if [ ! -e "$e" ] && [ ! -L "$e" ]; then continue; fi
    if [ "$e" = "$S" ] || [ "$e" = "$P" ]; then continue; fi
    mv "$e" "$2"/ || return 1
  done
}}
if [ -e "$P" ]; then
  echo "$P exists: an earlier restore stopped part way. The volume's previous contents are in it; inspect it and remove it, then restore again."
  exit {SWAP_LEFTOVER}
fi
rm -rf "$S" && mkdir "$S" || exit {SWAP_UNCHANGED}
if ! tar -xzpf - -C "$S"; then
  rm -rf "$S"; echo "the unpack failed; the volume is unchanged"; exit {SWAP_UNCHANGED}
fi
# The snapshot's `.` entry set the staging directory's owner and mode; the volume root takes them, so
# a non-root service (n8n, CouchDB) can still write at its data root, even in a newly created volume.
OLD_OWNER=$(stat -c %u:%g "$V") && OLD_MODE=$(stat -c %a "$V") \
  && NEW_OWNER=$(stat -c %u:%g "$S") && NEW_MODE=$(stat -c %a "$S") || {{ rm -rf "$S"; exit {SWAP_UNCHANGED}; }}
put_root_back() {{ chown "$OLD_OWNER" "$V"; chmod "$OLD_MODE" "$V"; }}
mkdir "$P" || {{ rm -rf "$S"; exit {SWAP_UNCHANGED}; }}
if chown "$NEW_OWNER" "$V" && chmod "$NEW_MODE" "$V" && move_all "$V" "$P" && move_all "$S" "$V"; then
  rmdir "$S" && rm -rf "$P"; exit 0
fi
if move_all "$V" "$S" && move_all "$P" "$V" && put_root_back; then
  rmdir "$P"; rm -rf "$S"; echo "a rename failed and was rolled back; the volume is unchanged"; exit {SWAP_UNCHANGED}
fi
echo "a rename failed and so did the rollback: the previous contents are in $P, the restored ones in $S"
exit {SWAP_ROLLBACK_FAILED}
"""
# A restore loads a dump into `<database>` + PG_TEMP_SUFFIX and renames the old one to
# `<database>` + PG_PREVIOUS_SUFFIX for the length of the swap transaction.
PG_TEMP_SUFFIX = "_ordo_restore"
PG_PREVIOUS_SUFFIX = "_ordo_previous"
PG_READY_TIMEOUT_SECONDS = 90


class BackupError(Exception):
    """A backup or restore that cannot proceed; the message says why and what to do."""


# ── planning (pure) ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class VolumePlan:
    """One named volume of the rendered stack and how it is saved and put back."""
    volume: str                     # the compose name (`qdrant-data`)
    docker_volume: str              # the Docker name (`ordo_qdrant-data`)
    method: str                     # one of backup_policy.METHODS
    declared: bool                  # False: no manifest declares it, method is the default
    writers: tuple[str, ...]        # services mounting it read-write
    readers: tuple[str, ...]        # services mounting it read-only
    database_service: str = ""      # pg_dump: the service running the server
    database_user: str = ""
    database_name: str = ""
    clients: tuple[str, ...] = ()   # pg_dump: the services that depend on the database service

    @property
    def mounted_by(self) -> tuple[str, ...]:
        return tuple(sorted({*self.writers, *self.readers}))

    @property
    def member(self) -> str:
        """The archive member holding this volume ("" for a skipped one)."""
        if self.method == backup_policy.PG_DUMP:
            return f"databases/{self.volume}.pgdump"
        if self.method in (backup_policy.STOPPED, backup_policy.LIVE):
            return f"volumes/{self.volume}.tar.gz"
        return ""

    def stopped_for_backup(self) -> tuple[str, ...]:
        """The services a backup stops while it reads the volume."""
        return self.writers if self.method == backup_policy.STOPPED else ()

    def stopped_for_restore(self) -> tuple[str, ...]:
        """The services a restore stops while it replaces the contents. Read-only mounters keep
        running: they write nothing, and they see the restored files once the copy is done."""
        if self.method == backup_policy.PG_DUMP:
            return self.clients
        if self.method in (backup_policy.STOPPED, backup_policy.LIVE):
            return self.writers
        return ()


def _mounts(spec: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """(named volume, read-only) for every named-volume mount of a compose service, in the short
    (`src:dst[:ro]`) and the long (`{type: volume, source, read_only}`) form."""
    found = []
    for mount in spec.get("volumes") or []:
        if isinstance(mount, str):
            name = backup_policy.named_volume(mount)
            if name:
                found.append((name, mount.count(":") >= 2 and "ro" in mount.rsplit(":", 1)[1].split(",")))
        elif isinstance(mount, Mapping) and mount.get("type", "volume") == "volume" and mount.get("source"):
            found.append((str(mount["source"]), bool(mount.get("read_only"))))
    return found


def _depends_on(spec: Mapping[str, Any]) -> list[str]:
    raw = spec.get("depends_on") or []
    return [str(name) for name in (raw.keys() if isinstance(raw, Mapping) else raw)]


def docker_volume_name(doc: Mapping[str, Any], project: str, volume: str) -> str:
    """The Docker name of a compose volume: its top-level `name:` when set, else compose's
    `<project>_<volume>`."""
    declared = (doc.get("volumes") or {}).get(volume) or {}
    return str(declared.get("name") or f"{project}_{volume}")


def plan(doc: Mapping[str, Any], policy: Mapping[str, str], *, project: str, env: Mapping[str, str],
         only: Sequence[str] | None = None) -> list[VolumePlan]:
    """Every named volume of the rendered compose `doc`, with its method from `policy` (the render's
    manifest `backup:` map) and the services involved. `only` keeps the volumes those services mount.

    Raises BackupError for an unknown `only` service, a pg_dump volume that is not mounted by
    exactly one service, and any plan that would stop ops-controller (the control plane holding
    the GPU lease is never stopped from here)."""
    services = stack.services_of(doc)
    unknown = sorted(set(only or ()) - set(services))
    if unknown:
        raise BackupError(f"no such service in the rendered stack: {', '.join(unknown)}")
    mounts: dict[str, list[tuple[str, bool]]] = {}
    for name, spec in services.items():
        for volume, read_only in _mounts(spec or {}):
            mounts.setdefault(volume, []).append((name, read_only))

    plans = []
    for volume in doc.get("volumes") or {}:
        users = mounts.get(volume, [])
        writers = tuple(sorted({name for name, read_only in users if not read_only}))
        readers = tuple(sorted({name for name, read_only in users if read_only} - set(writers)))
        method = policy.get(volume, backup_policy.UNDECLARED_DEFAULT)
        extra: dict[str, Any] = {}
        if method == backup_policy.PG_DUMP:
            if len(writers) != 1:
                raise BackupError(f"volume {volume} is declared pg_dump but is written by "
                                  f"{list(writers) or 'no service'}; a database volume has exactly one server")
            database = writers[0]
            environment = (services[database] or {}).get("environment") or {}
            if isinstance(environment, list):
                environment = dict(item.split("=", 1) for item in environment if "=" in item)
            user = stack.expand_env(str(environment.get("POSTGRES_USER") or "postgres"), dict(env))
            extra = {
                "database_service": database,
                "database_user": user,
                "database_name": stack.expand_env(str(environment.get("POSTGRES_DB") or user), dict(env)),
                "clients": tuple(sorted(name for name, spec in services.items()
                                        if database in _depends_on(spec or {}))),
            }
        plans.append(VolumePlan(volume=volume, docker_volume=docker_volume_name(doc, project, volume),
                                method=method, declared=volume in policy, writers=writers,
                                readers=readers, **extra))
    if only:
        # By writer only: a service that mounts a volume read-only (evals reads hermes-home) does not
        # own it, and naming it must not replace the volume under the service that does.
        wanted = set(only)
        plans = [p for p in plans if wanted & set(p.writers)]
        if not plans:
            raise BackupError(f"{', '.join(sorted(wanted))} write no volume of the rendered stack "
                              f"(--only selects the volumes a service writes)")
    for p in plans:
        for service in (*p.stopped_for_backup(), *p.stopped_for_restore()):
            if OPS_CONTROLLER_SERVICE in stack.lifecycle_group(doc, service):
                hint = ("its manifest must declare a method that does not stop its writer" if p.declared else
                        "no backup method is declared for it in the render's manifest.json (a render older than "
                        "the `backup:` declarations re-renders with `ordo render`)")
                raise BackupError(f"volume {p.volume} ({p.method}) would stop {OPS_CONTROLLER_SERVICE}, the "
                                  f"control plane: {hint}")
    return plans


def stop_set(doc: Mapping[str, Any], services: Iterable[str]) -> list[str]:
    """`services` with the services living in their network namespaces (stack.lifecycle_group):
    stopping an owner strands its members, so they stop and start with it."""
    group: list[str] = []
    for service in services:
        group += [name for name in stack.lifecycle_group(dict(doc), service) if name not in group]
    return group


def describe(doc: Mapping[str, Any], plans: Sequence[VolumePlan], *, restore: bool) -> list[str]:
    """The plan as printed lines: one per volume, with the services it stops (netns members included)."""
    lines = []
    for p in plans:
        note = "" if p.declared else " (no manifest declares it: the default)"
        stops = stop_set(doc, p.stopped_for_restore() if restore else p.stopped_for_backup())
        what = {
            backup_policy.PG_DUMP: f"pg_dump of {p.database_name} in {p.database_service}"
                                   + (" (loaded beside it, then swapped in)" if restore else ""),
            backup_policy.STOPPED: "file restore" if restore else "file snapshot, writers stopped",
            backup_policy.LIVE: "file restore" if restore else "file snapshot, services running",
            backup_policy.SKIP: "not backed up (re-derivable)",
        }[p.method]
        stop_text = f"; stops {', '.join(stops)}" if stops else ""
        lines.append(f"  [{p.method}{note}] {p.volume}: {what}{stop_text}")
    return lines


# ── destination and file permissions ────────────────────────────────────────────────────────────


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_destination(dest: Path, stack_dir: Path) -> None:
    """Refuse a destination inside the checkout or the rendered stack directory: the archive holds
    every secret, and neither is a place for it."""
    for parent, what in ((REPO_ROOT, "the ordo checkout"), (stack_dir, "the rendered stack directory")):
        if _inside(dest, parent):
            raise BackupError(f"refusing to write a backup inside {what} ({parent.resolve()}): it holds "
                              f"secrets. Pass --out with a directory outside it (default {DEFAULT_BACKUP_DIR})")


def restrict_to_owner(path: Path) -> str:
    """Make `path` readable by its owner only, and say how. On Windows `chmod` sets no access
    control, so the file's ACL is replaced with one granting the current user alone (icacls)."""
    if os.name == "nt":  # pragma: no cover - Windows only
        user = os.environ.get("USERNAME", "")
        grant = f"{user}:(OI)(CI)F" if path.is_dir() else f"{user}:F"
        proc = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                              capture_output=True, text=True)
        if not user or proc.returncode != 0:
            return (f"WARNING: could not restrict {path} to {user or 'the current user'} "
                    f"({(proc.stderr or proc.stdout).strip()}); restrict it by hand")
        return f"access limited to {user} (NTFS ACL)"
    path.chmod(0o700 if path.is_dir() else 0o600)
    return "access limited to the owner (mode 700/600)"


# ── the archive ─────────────────────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(archive: Path) -> dict[str, Any]:
    """The archive's manifest. Raises BackupError when it is not an ordo backup this version reads."""
    try:
        with tarfile.open(archive, "r:") as tar:
            member = tar.extractfile(MANIFEST_MEMBER)
            if member is None:
                raise KeyError(MANIFEST_MEMBER)
            manifest = json.loads(member.read().decode("utf-8"))
    except (OSError, tarfile.TarError, KeyError, ValueError) as e:
        raise BackupError(f"{archive} is not a readable ordo backup ({e})") from e
    if manifest.get("format") != ARCHIVE_FORMAT:
        raise BackupError(f"{archive} is backup format {manifest.get('format')!r}; this ordo reads "
                          f"format {ARCHIVE_FORMAT}")
    return manifest


def verify(archive: Path, members: Iterable[str]) -> None:
    """Check each named member against the sha256 the manifest recorded, before anything is changed."""
    manifest = read_manifest(archive)
    expected = {entry["member"]: entry["sha256"]
                for entry in [*manifest.get("config", []), *manifest.get("volumes", [])] if entry.get("member")}
    with tarfile.open(archive, "r:") as tar:
        for name in members:
            if name not in expected:
                raise BackupError(f"{archive}: the manifest lists no checksum for {name}")
            try:
                f = tar.extractfile(name)
            except KeyError as e:
                raise BackupError(f"{archive} is missing {name}, which its manifest lists") from e
            if f is None:
                raise BackupError(f"{archive}: {name} is not a file")
            digest = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
            if digest.hexdigest() != expected[name]:
                raise BackupError(f"{archive}: {name} does not match its recorded sha256 (a damaged archive)")


def restore_config(archive: Path, manifest: Mapping[str, Any], stack_dir: Path, *, dry_run: bool) -> list[str]:
    """Put each config file back where it is absent. A present one is never overwritten: identical is
    reported unchanged, different is kept and reported (the archive's copy stays in the archive)."""
    lines = []
    with tarfile.open(archive, "r:") as tar:
        for entry in manifest.get("config", []):
            name, target = entry["file"], stack_dir / entry["file"]
            f = tar.extractfile(entry["member"])
            data = f.read() if f is not None else b""
            if target.exists():
                state = ("unchanged" if target.read_bytes() == data else
                         f"kept: {target} differs from the backup (extract {entry['member']} from the archive "
                         f"to compare)")
                lines.append(f"  [config] {name}: {state}")
                continue
            lines.append(f"  [config] {name}: {'would restore' if dry_run else 'restored'} to {target}")
            if not dry_run:
                stack_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if name in SECRET_CONFIG_FILES:
                    restrict_to_owner(target)
    return lines


# ── docker ──────────────────────────────────────────────────────────────────────────────────────


class Docker:  # pragma: no cover - shells to docker; exercised end to end (see the PR evidence)
    """The docker calls a backup and a restore make, in one place so tests can replace them."""

    def __init__(self, compose_dir: Path, project: str, doc: Mapping[str, Any]):
        self.compose_dir = compose_dir.resolve().as_posix()
        self.project = project
        self.doc = doc

    def _run(self, argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
        sys.stdout.flush()  # keep this command's own output after what was printed before it
        try:
            return subprocess.run(list(argv), **kw)
        except (OSError, subprocess.SubprocessError) as e:
            raise BackupError(f"cannot run {argv[0]}: {e}") from e

    def running_container(self, service: str) -> str | None:
        try:
            return bringup.find_running_container(self.project, service)
        except bringup.LeaseUnknown as e:
            raise BackupError(str(e)) from e

    def compose(self, *args: str) -> None:
        cmd = stack.compose_argv(self.compose_dir, self.project, *args,
                                 profiles=stack.profiles_in(dict(self.doc)))
        print(f"$ {shlex.join(cmd)}")
        proc = self._run(cmd)
        if proc.returncode != 0:
            raise BackupError(f"docker compose {args[0]} failed (exit {proc.returncode})")

    def volume_exists(self, docker_volume: str) -> bool:
        return self._run(["docker", "volume", "inspect", docker_volume], capture_output=True).returncode == 0

    def create_volume(self, docker_volume: str, volume: str) -> None:
        """Create it with the labels compose gives its own, so the stack adopts it."""
        proc = self._run(["docker", "volume", "create",
                          "--label", f"com.docker.compose.project={self.project}",
                          "--label", f"com.docker.compose.volume={volume}", docker_volume],
                         capture_output=True, text=True)
        if proc.returncode != 0:
            raise BackupError(f"cannot create volume {docker_volume}: {proc.stderr.strip()}")

    def image_of(self, service: str) -> dict[str, Any]:
        """The rendered image reference of `service`, and the image id and repo digests of its
        running container when there is one."""
        spec = stack.services_of(dict(self.doc)).get(service) or {}
        env = _rendered_env(Path(self.compose_dir))
        record: dict[str, Any] = {"service": service, "image": stack.expand_env(str(spec.get("image", "")), env)}
        container = self.running_container(service)
        if container:
            proc = self._run(["docker", "inspect", "--format", "{{.Image}}", container],
                             capture_output=True, text=True)
            image_id = proc.stdout.strip() if proc.returncode == 0 else ""
            record["image_id"] = image_id
            if image_id:
                proc = self._run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id],
                                 capture_output=True, text=True)
                record["repo_digests"] = json.loads(proc.stdout or "[]") if proc.returncode == 0 else []
        return record

    def _stream_out(self, argv: Sequence[str], dest: IO[bytes], what: str) -> None:
        proc = self._run(argv, stdout=dest, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise BackupError(f"{what} failed (exit {proc.returncode}): "
                              f"{proc.stderr.decode('utf-8', 'replace').strip()[-400:]}")

    def _stream_in(self, argv: Sequence[str], source: IO[bytes], what: str) -> str:
        """Run `argv` with `source` on its stdin and return its output; a failure raises BackupError."""
        returncode, text = self._pipe(argv, source)
        if returncode != 0:
            raise BackupError(f"{what} failed (exit {returncode}): {text[-400:]}")
        return text

    def _pipe(self, argv: Sequence[str], source: IO[bytes]) -> tuple[int, str]:
        """Run `argv` with `source` on its stdin: (exit code, output). `source` is usually an archive
        member, which has no OS file handle, so it is copied through a pipe; the output goes to a
        temporary file so a chatty process cannot block on a full stdout pipe while its stdin is
        still being written."""
        sys.stdout.flush()
        with tempfile.TemporaryFile() as output:
            try:
                proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
            except (OSError, subprocess.SubprocessError) as e:
                raise BackupError(f"cannot run {argv[0]}: {e}") from e
            assert proc.stdin is not None
            try:
                shutil.copyfileobj(source, proc.stdin, 1 << 20)
            except (BrokenPipeError, OSError):
                pass  # the process stopped reading: its exit code and output say why
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            returncode = proc.wait()
            output.seek(0)
            text = output.read().decode("utf-8", "replace").strip()
        return returncode, text

    def snapshot_volume(self, docker_volume: str, dest: IO[bytes]) -> None:
        self._stream_out(["docker", "run", "--rm", "--user", "0", "--network", "none",
                          "-v", f"{docker_volume}:{VOLUME_MOUNT}:ro", "--entrypoint", "tar", HELPER_IMAGE,
                          "-czf", "-", "-C", VOLUME_MOUNT, "."], dest, f"snapshot of {docker_volume}")

    def replace_volume(self, docker_volume: str, source: IO[bytes]) -> None:
        """Swap the snapshot in for the volume's contents (REPLACE_VOLUME_SCRIPT), in one helper container."""
        code, text = self._pipe(["docker", "run", "--rm", "-i", "--user", "0", "--network", "none",
                                 "-v", f"{docker_volume}:{VOLUME_MOUNT}", "--entrypoint", "sh", HELPER_IMAGE,
                                 "-c", REPLACE_VOLUME_SCRIPT, "sh", VOLUME_MOUNT], source)
        if code == 0:
            return
        # The script's own last line states the volume's state for the codes it chooses.
        known = code in (SWAP_LEFTOVER, SWAP_UNCHANGED, SWAP_ROLLBACK_FAILED)
        state = "" if known else "; the volume's state is unknown: inspect it before starting its services"
        raise BackupError(f"restore of {docker_volume} failed (exit {code}): {text[-400:]}{state}")

    def pg_dump(self, container: str, user: str, database: str, dest: IO[bytes]) -> None:
        self._stream_out(["docker", "exec", container, "pg_dump", "-U", user, "-d", database, "-Fc"],
                         dest, f"pg_dump of {database} in {container}")

    def _psql(self, container: str, user: str, script: str, what: str, **variables: str) -> str:
        """Run a psql script (from stdin, so `:"name"` / `:'name'` variables are quoted by psql)
        against template1, stopping at the first error. template1 is never a restore target."""
        argv = ["docker", "exec", "-i", container, "psql", "-U", user, "-d", "template1", "-q", "-tA",
                "-v", "ON_ERROR_STOP=1"]
        for name, value in variables.items():
            argv += ["-v", f"{name}={value}"]
        return self._stream_in(argv, io.BytesIO(script.encode("utf-8")), what)

    def pg_server_major(self, container: str, user: str) -> int:
        text = self._psql(container, user, "SHOW server_version_num;\n", f"reading the server version of {container}")
        return int(text.strip().splitlines()[-1]) // 10000

    def pg_dump_major(self, container: str, user: str, source: IO[bytes]) -> int:
        """The major version of the server a dump came from, read by the target's own pg_restore
        (which also proves it can read the dump's format)."""
        text = self._stream_in(["docker", "exec", "-i", container, "pg_restore", "--list"], source,
                               f"reading the dump with {container}'s pg_restore")
        match = re.search(r"Dumped from database version: (\d+)", text)
        if match is None:
            raise BackupError(f"{container}'s pg_restore --list names no source version for the dump")
        return int(match.group(1))

    def pg_restore_to_temp(self, container: str, user: str, database: str, source: IO[bytes]) -> None:
        """Load the dump into a fresh `<database>_ordo_restore`, leaving `<database>` untouched. A
        load that fails drops the temporary database again."""
        temp, previous = database + PG_TEMP_SUFFIX, database + PG_PREVIOUS_SUFFIX
        names = self._psql(container, user, "SELECT datname FROM pg_database;\n", "listing the databases").split()
        if previous in names:
            raise BackupError(f"database {previous} exists in {container}: an earlier restore stopped part way. "
                              f"Inspect it and drop it, then restore again; {database} is unchanged")
        self._psql(container, user, 'DROP DATABASE IF EXISTS :"temp" WITH (FORCE);\n'
                                    'CREATE DATABASE :"temp" TEMPLATE template0;\n',
                   f"creating {temp}", temp=temp)
        code, text = self._pipe(["docker", "exec", "-i", container, "pg_restore", "-U", user, "-d", temp,
                                 "--exit-on-error", "--single-transaction"], source)
        if code != 0:
            self._psql(container, user, 'DROP DATABASE IF EXISTS :"temp" WITH (FORCE);\n', f"dropping {temp}",
                       temp=temp)
            raise BackupError(f"pg_restore of {database} failed (exit {code}): {text[-400:]}; {database} is unchanged")

    def pg_swap(self, container: str, user: str, database: str) -> None:
        """Rename the loaded temporary database over `database` in one transaction, then drop the old one."""
        temp, previous = database + PG_TEMP_SUFFIX, database + PG_PREVIOUS_SUFFIX
        self._psql(container, user,
                   "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   "WHERE datname IN (:'target', :'temp') AND pid <> pg_backend_pid();\n"
                   "BEGIN;\n"
                   'ALTER DATABASE :"target" RENAME TO :"previous";\n'
                   'ALTER DATABASE :"temp" RENAME TO :"target";\n'
                   "COMMIT;\n",
                   f"swapping {temp} in for {database} (one transaction: on failure {database} is unchanged)",
                   target=database, temp=temp, previous=previous)
        try:
            self._psql(container, user, 'DROP DATABASE :"previous" WITH (FORCE);\n', f"dropping {previous}",
                       previous=previous)
        except BackupError as e:
            print(f"  warning: {database} is restored, but the old copy {previous} was not dropped ({e}); "
                  f"drop it by hand before the next restore", file=sys.stderr)

    def wait_pg_ready(self, container: str, user: str, database: str) -> None:
        """Wait until the server answers on TCP. The image's first start runs initdb behind a
        temporary server that listens on the local socket only, then restarts it: a socket probe
        would pass during that window and the restore would hit the restart."""
        deadline = time.monotonic() + PG_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            proc = self._run(["docker", "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", user, "-d", database],
                             capture_output=True)
            if proc.returncode == 0:
                return
            time.sleep(1)
        raise BackupError(f"{container} did not accept connections within {PG_READY_TIMEOUT_SECONDS}s")

    def bring_up(self, services: Sequence[str]) -> None:
        """Create and start services that have no container (the lease-checked `ordo up` path)."""
        code = bringup.bring_up(self.compose_dir, self.project, services, whole_stack=False,
                                with_profiles=True, force_recreate=False, dry_run=False)
        if code != 0:
            raise BackupError(f"could not start {', '.join(services)} (exit {code})")


def _rendered_env(stack_dir: Path) -> dict[str, str]:
    env_file = stack_dir / ".env"
    return parity.load_env(str(env_file)) if env_file.exists() else {}


def load_stack(stack_dir: Path) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """(rendered compose, rendered .env, backup policy) of the stack in `stack_dir`."""
    try:
        doc = stack.load_compose(stack_dir.resolve().as_posix())
    except (OSError, ValueError) as e:
        raise BackupError(f"cannot read the rendered stack in {stack_dir} ({e}); render first: "
                          f"ordo render --out {stack_dir}") from e
    manifest_path = stack_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except ValueError as e:
        raise BackupError(f"cannot read {manifest_path} ({e}); re-render: ordo render --out {stack_dir}") from e
    return doc, _rendered_env(stack_dir), dict(manifest.get("backup") or {})


def _running(docker: Docker, services: Iterable[str]) -> list[str]:
    return [s for s in services if docker.running_container(s)]


def _start(docker: Docker, project: str, services: Sequence[str]) -> None:
    """Start stopped services again (`compose start`: the same containers, never recreated), after
    the GPU lease check every host bring-up makes."""
    if not services:
        return
    try:
        gpu = bringup.read_gpu_status(project)
    except bringup.LeaseUnknown as e:
        raise BackupError(f"not starting {', '.join(services)}: {e}. Start them with "
                          f"`ordo up {' '.join(services)}` once the lease state is readable") from e
    refusal = bringup.lease_refusal(gpu, whole_stack=False, starts=set(services))
    if refusal:
        raise BackupError(f"not starting {', '.join(services)}: {refusal}")
    docker.compose("start", *services)


class _StoppedServices:
    """Stop the running ones of `services` (with their netns members) for a `with` block, then
    start them again.

    Each stop is recorded as it succeeds, so a stop that fails part way starts again exactly what
    it had stopped (nothing was changed yet). When the block itself fails, `restart_on_failure`
    decides: a backup changed nothing and restarts; a restore may have left its target in any
    state, so the services stay stopped and the operator is told what to do."""

    def __init__(self, docker: Docker, project: str, services: Sequence[str], *, restart_on_failure: bool):
        self.docker, self.project = docker, project
        self.targets = _running(docker, stop_set(docker.doc, services))
        self.restart_on_failure = restart_on_failure
        self.stopped: list[str] = []

    def __enter__(self) -> _StoppedServices:
        try:
            for service in self.targets:
                self.docker.compose("stop", service)
                self.stopped.append(service)
        except BackupError:
            self._start_again(failed=True)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None or self.restart_on_failure:
            self._start_again(failed=exc_type is not None)
            return
        if self.stopped:
            names = " ".join(self.stopped)
            print(f"left stopped: {', '.join(self.stopped)}. The restore error says what state the data is in. "
                  f"Fix its cause and run the restore again, or start them on the data as it is: "
                  f"ordo up {names}", file=sys.stderr)

    def _start_again(self, *, failed: bool) -> None:
        try:
            _start(self.docker, self.project, self.stopped)
        except BackupError as e:
            if not failed:
                raise
            # Something already failed: report this too, and let the first error be the one raised.
            print(f"also: {e}", file=sys.stderr)


# ── backup ──────────────────────────────────────────────────────────────────────────────────────


def _git_commit() -> str:
    try:
        proc = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _add_file(tar: tarfile.TarFile, path: Path, member: str) -> dict[str, Any]:
    def owner_only(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.mode = 0o600  # extracted by hand, a member is still readable by its owner only
        return info

    tar.add(str(path), arcname=member, recursive=False, filter=owner_only)
    return {"member": member, "sha256": _sha256_file(path), "bytes": path.stat().st_size}


def run_backup(*, stack_dir: Path, project: str, dest: Path, only: Sequence[str] | None, dry_run: bool,
               docker: Docker | None = None, now: datetime.datetime | None = None) -> Path | None:
    """Write one archive of the stack's state to `dest`; returns its path (None on a dry run)."""
    check_destination(dest, stack_dir)
    doc, env, policy = load_stack(stack_dir)
    plans = plan(doc, policy, project=project, env=env, only=only)
    config = [] if only else [name for name in CONFIG_FILES if (stack_dir / name).is_file()]
    print(f"backup of project {project} from {stack_dir.resolve()}:")
    for name in config:
        print(f"  [config] {name}")
    for line in describe(doc, plans, restore=False):
        print(line)
    if dry_run:
        return None
    docker = docker or Docker(stack_dir, project, doc)
    stamp = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y%m%dT%H%M%SZ")
    dest.mkdir(parents=True, exist_ok=True)
    access = restrict_to_owner(dest)
    archive = dest / f"ordo-backup-{project}-{stamp}.tar"
    partial = dest / f".{archive.name}.partial"
    scratch = dest / f".{archive.name}.member"
    stack_manifest = stack_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "created_utc": stamp,
        "project": project,
        "ordo_commit": _git_commit(),
        "substrate_digest": (json.loads(stack_manifest.read_text(encoding="utf-8")).get("substrate_digest", "")
                             if stack_manifest.exists() else ""),
        "contains_secrets": "secrets.env" in config,
        "config": [],
        "volumes": [],
    }
    try:
        with tarfile.open(partial, "w:") as tar:
            restrict_to_owner(partial)
            for name in config:
                manifest["config"].append({"file": name, **_add_file(tar, stack_dir / name, f"config/{name}")})
            # Online dumps and live snapshots first, then the ones that stop services.
            order = {backup_policy.PG_DUMP: 0, backup_policy.LIVE: 1, backup_policy.STOPPED: 2, backup_policy.SKIP: 3}
            for p in sorted(plans, key=lambda p: order[p.method]):
                manifest["volumes"].append(_backup_volume(docker, project, p, tar, scratch))
            _add_bytes(tar, MANIFEST_MEMBER, json.dumps(manifest, indent=2).encode("utf-8"))
        partial.replace(archive)
    finally:
        for leftover in (partial, scratch):
            if leftover.exists():
                leftover.unlink()
    access_file = restrict_to_owner(archive)
    saved = sum(1 for v in manifest["volumes"] if v["status"] == "saved")
    print(f"\nwrote {archive} ({archive.stat().st_size} bytes; {saved} volume(s), {len(config)} config file(s))")
    print(f"it holds secrets: {access_file}; directory {access}. Keep a copy off this machine.")
    return archive


def _add_bytes(tar: tarfile.TarFile, member: str, data: bytes) -> None:
    info = tarfile.TarInfo(member)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _backup_volume(docker: Docker, project: str, p: VolumePlan, tar: tarfile.TarFile,
                   scratch: Path) -> dict[str, Any]:
    record: dict[str, Any] = {"volume": p.volume, "docker_volume": p.docker_volume, "method": p.method,
                              "declared": p.declared, "mounted_by": list(p.mounted_by)}
    if p.method == backup_policy.SKIP:
        return {**record, "status": "skipped"}
    if p.method == backup_policy.PG_DUMP:
        container = docker.running_container(p.database_service)
        if container is None:
            raise BackupError(f"{p.database_service} is not running, so {p.volume} cannot be dumped; start it "
                              f"(`ordo up {p.database_service}`) or leave it out with --only")
        record["database"] = {"service": p.database_service, "user": p.database_user, "name": p.database_name}
        record["images"] = [docker.image_of(p.database_service)]
        print(f"  dumping {p.database_name} from {container} ...")
        with scratch.open("wb") as f:
            docker.pg_dump(container, p.database_user, p.database_name, f)
    else:
        if not docker.volume_exists(p.docker_volume):
            print(f"  {p.docker_volume} does not exist (never started): nothing to save")
            return {**record, "status": "absent"}
        record["images"] = [docker.image_of(s) for s in p.writers]
        with _StoppedServices(docker, project, p.stopped_for_backup(), restart_on_failure=True):
            print(f"  saving {p.docker_volume} ...")
            with scratch.open("wb") as f:
                docker.snapshot_volume(p.docker_volume, f)
    entry = _add_file(tar, scratch, p.member)
    scratch.unlink()
    return {**record, **entry, "status": "saved"}


# ── restore ─────────────────────────────────────────────────────────────────────────────────────


def _image_changes(entry: Mapping[str, Any], doc: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """The services whose rendered image differs from the one that wrote this snapshot."""
    services = stack.services_of(dict(doc))
    changes = []
    for record in entry.get("images") or []:
        now = stack.expand_env(str((services.get(record["service"]) or {}).get("image", "")), dict(env))
        if now and now != record.get("image"):
            changes.append(f"{record['service']}: backup {record.get('image')} -> rendered {now}")
    return changes


def run_restore(*, archive: Path, stack_dir: Path, project: str, only: Sequence[str] | None, dry_run: bool,
                allow_image_change: bool = False, docker: Docker | None = None) -> int:
    """Put the archive's state back into the stack. Exit codes: 0 done, 1 refused or failed,
    2 refused because of the GPU lease (as `ordo up`)."""
    manifest = read_manifest(archive)
    if manifest.get("project") != project:
        raise BackupError(f"{archive} is a backup of project {manifest.get('project')!r}, not {project!r} "
                          f"(pass --project {manifest.get('project')} to restore it there)")
    print(f"restore of {archive} (made {manifest.get('created_utc')}, ordo {manifest.get('ordo_commit') or '?'}) "
          f"into project {project}:")
    entries = [e for e in manifest.get("volumes", []) if e.get("status") == "saved"]
    config_members = [] if only else [e["member"] for e in manifest.get("config", [])]
    verify(archive, [*config_members, *(e["member"] for e in entries)])
    print("  archive checksums verified")
    if not only:
        for line in restore_config(archive, manifest, stack_dir, dry_run=dry_run):
            print(line)

    if not (stack_dir / stack.COMPOSE_FILE).exists():
        if not entries:
            return 0
        print(f"\nno rendered stack in {stack_dir}: the volumes are not restored yet. Render and start the "
              f"stack (`ordo apply`, or `ordo render` then `ordo up --all`), then run this restore again.",
              file=sys.stderr)
        return 1
    doc, env, policy = load_stack(stack_dir)
    current = {p.volume: p for p in plan(doc, policy, project=project, env=env, only=only)}
    if only:
        entries = [e for e in entries if e["volume"] in current]
    todo: list[tuple[dict[str, Any], VolumePlan]] = []
    for entry in entries:
        p = current.get(entry["volume"])
        if p is None:
            print(f"  [{entry['method']}] {entry['volume']}: not in the rendered stack now; left in the archive")
            continue
        if p.method != entry["method"] and backup_policy.PG_DUMP in (p.method, entry["method"]):
            raise BackupError(f"{entry['volume']} was saved as {entry['method']} but is declared {p.method} now; "
                              f"a dump and a file snapshot are not interchangeable")
        changes = _image_changes(entry, doc, env)
        if changes and not allow_image_change:
            raise BackupError(f"{entry['volume']} was written by a different image than the rendered one "
                              f"({'; '.join(changes)}). Restore it with the image it came from, or pass "
                              f"--allow-image-change if that software reads the older data (a dump from a "
                              f"newer PostgreSQL major is refused either way)")
        for change in changes:
            print(f"  note: {entry['volume']}: {change}")
        todo.append((entry, p))
    for line in describe(doc, [p for _entry, p in todo], restore=True):
        print(line)

    try:
        gpu = bringup.read_gpu_status(project)
    except bringup.LeaseUnknown as e:
        print(f"refusing: {e}. A restore stops and starts services, so it needs the lease state.",
              file=sys.stderr)
        return 2
    if gpu is not None and bringup.is_leased(gpu):
        print(f"refusing to restore while the GPU is leased ({bringup._holders(gpu)}). Wait for the lease "
              f"to end, then run the restore again.", file=sys.stderr)
        return 2
    if dry_run:
        print("\ndry run: nothing changed")
        return 0

    docker = docker or Docker(stack_dir, project, doc)
    with tarfile.open(archive, "r:") as tar:
        for entry, p in todo:
            def open_member(member: str = entry["member"]) -> IO[bytes]:
                source = tar.extractfile(member)
                if source is None:
                    raise BackupError(f"{archive}: {member} is not a file")
                return source

            if p.method == backup_policy.PG_DUMP:
                _restore_database(docker, project, p, open_member)
            else:
                source = open_member()
                with _StoppedServices(docker, project, p.stopped_for_restore(), restart_on_failure=False):
                    if not docker.volume_exists(p.docker_volume):
                        docker.create_volume(p.docker_volume, p.volume)
                    print(f"  restoring {p.docker_volume} ...")
                    docker.replace_volume(p.docker_volume, source)
    print(f"\nrestored {len(todo)} volume(s)")
    return 0


def _restore_database(docker: Docker, project: str, p: VolumePlan, open_dump: Callable[[], IO[bytes]]) -> None:
    """Replace the database from the dump. The target's pg_restore reads the dump first (a dump
    from a newer PostgreSQL major is refused), the dump is loaded into a temporary database beside
    the live one while its clients keep running, and only the swap (one transaction) runs with the
    clients stopped. A server that is not running is started for the restore (the lease-checked
    `ordo up` path) and stopped after."""
    started_here = False
    container = docker.running_container(p.database_service)
    if container is None:
        docker.bring_up([p.database_service])
        started_here = True
        container = docker.running_container(p.database_service)
        if container is None:
            raise BackupError(f"{p.database_service} did not start")
    try:
        docker.wait_pg_ready(container, p.database_user, p.database_name)
        server = docker.pg_server_major(container, p.database_user)
        try:
            dumped = docker.pg_dump_major(container, p.database_user, open_dump())
        except BackupError as e:
            raise BackupError(f"{e}; {p.database_name} is unchanged") from e
        if dumped > server:
            raise BackupError(f"{p.volume} is a dump from PostgreSQL {dumped}, and {p.database_service} runs "
                              f"PostgreSQL {server}: an older server cannot load it. {p.database_name} is unchanged")
        print(f"  loading {p.database_name} into {p.database_name}{PG_TEMP_SUFFIX} in {container} ...")
        docker.pg_restore_to_temp(container, p.database_user, p.database_name, open_dump())
        with _StoppedServices(docker, project, p.clients, restart_on_failure=False):
            print(f"  swapping it in for {p.database_name} ...")
            docker.pg_swap(container, p.database_user, p.database_name)
    finally:
        if started_here:
            docker.compose("stop", p.database_service)
