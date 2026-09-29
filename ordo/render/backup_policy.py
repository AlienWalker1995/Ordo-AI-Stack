"""How each named volume is backed up: the declaration every manifest makes for the volumes it owns.

A manifest (a plugin's `plugin.yaml`, an agent's `agent.yaml`, and `compose.CORE_BACKUP` for the
core services) declares `backup: {<volume>: <method>}` for each named volume its services write.
The render collects the declarations of what it enables into `out/manifest.json` (`backup:`), and
`ordo backup` / `ordo restore` (ordo/host/backup.py) read them from there, so the policy lives next
to the volume it describes and a new volume ships with its backup decision.

The methods:
  - `pg_dump`: a PostgreSQL data directory. Dumped online with the server's own `pg_dump`
    (custom format); a restore drops the database and re-creates it from the dump with
    `pg_restore --create`, with the database's clients stopped.
  - `stopped`: a snapshot of the volume's files taken while every service writing it is stopped
    (any database without a dump tool in its image: SQLite, Qdrant, CouchDB, ClickHouse, Redis...).
  - `live`: a snapshot taken while the service runs; only for files that are never mid-write
    (certificates, node keys, an application tree).
  - `skip`: not backed up; the contents are re-derivable (model weights, caches, metrics, logs).

A volume the render mounts with no declaration is backed up `stopped`, the method that is correct
for any contents; tests/substrate/test_backup.py requires every shipped volume to declare one.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

PG_DUMP = "pg_dump"
STOPPED = "stopped"
LIVE = "live"
SKIP = "skip"
METHODS = (PG_DUMP, STOPPED, LIVE, SKIP)
# What a volume nobody declared gets: a cold snapshot is a correct backup of anything.
UNDECLARED_DEFAULT = STOPPED


def named_volume(spec: str) -> str | None:
    """The named volume a compose `src:dst[:mode]` mount uses, or None for a bind mount.

    The same rule compose.py applies when it declares the top-level `volumes:`: a bare name, not a
    `./` / absolute / `~` / `${VAR}` path."""
    if ":" not in spec:
        return None
    source = spec.split(":", 1)[0]
    if not source or source.startswith((".", "/", "~", "$")) or "/" in source:
        return None
    return source


def parse(where: str, raw: Any, mounts: Iterable[str]) -> dict[str, str]:
    """A manifest's `backup:` block, validated against the volume mounts its services declare.

    Each key must be a named volume one of `mounts` uses (a manifest declares only its own
    volumes), and each value one of METHODS."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: backup must be a mapping of volume name to one of {list(METHODS)} "
                         f"(got {raw!r})")
    own = {name for name in (named_volume(str(m)) for m in mounts) if name}
    policy: dict[str, str] = {}
    for volume, method in raw.items():
        volume, method = str(volume), str(method)
        if method not in METHODS:
            raise ValueError(f"{where}: backup.{volume} must be one of {list(METHODS)} (got {method!r})")
        if volume not in own:
            raise ValueError(f"{where}: backup names volume {volume!r}, which none of its services mount "
                             f"(its named volumes: {sorted(own) or 'none'})")
        policy[volume] = method
    return policy


def collect(declarations: Iterable[tuple[str, Mapping[str, str]]]) -> dict[str, str]:
    """Merge the (declared by, policy) pairs of everything the render enables into one map.

    A volume declared twice is refused: two owners could disagree about how it is backed up."""
    merged: dict[str, str] = {}
    owner: dict[str, str] = {}
    for declared_by, policy in declarations:
        for volume, method in policy.items():
            if volume in merged:
                raise ValueError(f"volume {volume!r} declares its backup twice ({owner[volume]} and "
                                 f"{declared_by}); keep it on the manifest of the service that writes it")
            merged[volume] = method
            owner[volume] = declared_by
    return merged
