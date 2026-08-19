"""Two-way federation between Hermes memory files and the Obsidian vault.

Reconciles volume-side files (~/.hermes/SOUL.md, memories/*.md) with their
materialized vault notes using a content-hash manifest. Operator (vault)
wins conflicts; agent versions are preserved, never silently lost; SOUL.md
is never deleted. Stdlib only. Must never raise to its caller.

Spec: docs/superpowers/specs/2026-08-19-memory-stack-overhaul-design.md
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

try:
    import fcntl
except ImportError:  # Windows test hosts / no fcntl support
    fcntl = None

LOCK_FRESH_SECONDS = 300


@dataclass(frozen=True)
class PairState:
    volume_text: str | None
    vault_text: str | None
    synced_hash: str | None
    volume_locked: bool = False


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def decide(pair: PairState, *, is_soul: bool) -> str:
    if pair.volume_locked:
        return "skip_locked"
    vol, note, synced = pair.volume_text, pair.vault_text, pair.synced_hash
    # A blank vault note (0 bytes or whitespace-only) is treated as absent so it
    # routes into the quarantine/restore_soul paths instead of a destructive pull
    # that would overwrite the agent's volume copy with nothing.
    if note is not None and note.strip() == "":
        note = None
    if vol is None and note is None:
        return "noop"
    if note is None:
        if vol is not None and synced is None:
            return "push"
        return "restore_soul" if is_soul else "quarantine"
    if vol is None:
        return "pull"
    hv, hn = content_hash(vol), content_hash(note)
    if hv == hn:
        return "noop"
    if synced is None:
        return "conflict_pull"
    if hv == synced:
        return "pull"
    if hn == synced:
        return "push"
    return "conflict_pull"


@dataclass(frozen=True)
class Pair:
    name: str
    volume: pathlib.Path
    vault: pathlib.Path
    is_soul: bool = False


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed so half-written temps inside the synced vault tree are
    # invisible to Obsidian / the bridge (both ignore dotfiles).
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _is_locked(volume: pathlib.Path) -> bool:
    lock = volume.with_name(volume.name + ".lock")
    if fcntl is not None:
        if not lock.exists():
            return False
        try:
            with open(lock, "rb") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True  # held by another process
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    return False
        except OSError:
            return True  # couldn't even open/probe it: fail toward skipping, safe
    # Windows test hosts (no fcntl): fall back to the mtime heuristic.
    try:
        return (time.time() - lock.stat().st_mtime) < LOCK_FRESH_SECONDS
    except FileNotFoundError:
        return False


def load_manifest(path: pathlib.Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_manifest(path: pathlib.Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def sync_pair(pair: Pair, manifest: dict, *, quarantine_dir: pathlib.Path,
              conflicts_dir: pathlib.Path, now_utc: str) -> str:
    state = PairState(
        volume_text=_read(pair.volume),
        vault_text=_read(pair.vault),
        synced_hash=(manifest.get(pair.name) or {}).get("hash"),
        volume_locked=_is_locked(pair.volume),
    )
    action = decide(state, is_soul=pair.is_soul)
    if action == "pull":
        _write(pair.volume, state.vault_text)
        manifest[pair.name] = {"hash": content_hash(state.vault_text)}
    elif action == "push":
        _write(pair.vault, state.volume_text)
        manifest[pair.name] = {"hash": content_hash(state.volume_text)}
    elif action == "conflict_pull":
        conflicts_dir.mkdir(parents=True, exist_ok=True)
        _write(conflicts_dir / f"{pair.name}-{now_utc}.md", state.volume_text)
        _write(pair.volume, state.vault_text)
        manifest[pair.name] = {"hash": content_hash(state.vault_text)}
    elif action == "quarantine":
        # Namespaced under a vault-federation subdir, timestamped per-file so
        # repeated quarantines never overwrite each other in the shared
        # _quarantine dir.
        dest_dir = quarantine_dir / "vault-federation"
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write(dest_dir / f"{pair.volume.name}-{now_utc}.md", state.volume_text)
        pair.volume.unlink(missing_ok=True)
        manifest.pop(pair.name, None)
    elif action == "restore_soul":
        _write(pair.vault, state.volume_text)
        manifest[pair.name] = {"hash": content_hash(state.volume_text)}
        conflicts_dir.mkdir(parents=True, exist_ok=True)
        _write(
            conflicts_dir / f"SOUL-restored-{now_utc}.md",
            "The SOUL vault note vanished and was restored from the agent's volume copy.\n",
        )
    elif action == "noop":
        if state.volume_text is not None:
            manifest[pair.name] = {"hash": content_hash(state.volume_text)}
        else:
            # Both sides missing: drop any stale manifest entry so a later
            # agent-recreated file isn't mistaken for a deletion and quarantined.
            manifest.pop(pair.name, None)
    return action


def default_pairs(hermes_home: pathlib.Path, vault_notes: pathlib.Path) -> list[Pair]:
    core = vault_notes / "Ordo" / "Context Files"
    return [
        Pair("SOUL", hermes_home / "SOUL.md", core / "SOUL.md", is_soul=True),
        Pair("MEMORY", hermes_home / "memories" / "MEMORY.md", core / "Agent Context.md"),
        Pair("USER", hermes_home / "memories" / "USER.md", core / "User Profile.md"),
    ]


def main() -> int:
    try:
        hermes_home = pathlib.Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
        vault_notes = pathlib.Path(os.environ.get("VAULT_NOTES", "/workspace/data/memory-vault/notes"))
        if not vault_notes.is_dir():
            print(f"vault-federate: vault notes dir missing: {vault_notes} - skipping federation")
            return 0  # bridge/profile not up: nothing to federate
        manifest_path = hermes_home / "state" / "vault-federation.json"
        manifest = load_manifest(manifest_path)
        if not (vault_notes / "Ordo" / "Context Files").is_dir() and manifest:
            # The vault tree isn't materialized (e.g. mid-mount, bridge not
            # finished seeding) but we already have a populated manifest from a
            # prior successful run. Treating this as "vault deleted everything"
            # would mass-quarantine every pair. Refuse instead of syncing.
            print("vault-federate: vault tree missing but manifest populated - refusing to run (would mass-quarantine)")
            return 0
        now_utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        conflicts = vault_notes / "Ordo" / "Context Files" / "Conflicts"
        for pair in default_pairs(hermes_home, vault_notes):
            try:
                action = sync_pair(pair, manifest, quarantine_dir=hermes_home / "_quarantine",
                                   conflicts_dir=conflicts, now_utc=now_utc)
                print(f"vault-federate: {pair.name}: {action}")
            except Exception as exc:  # per-pair isolation: one bad pair must not stop the rest
                print(f"vault-federate: {pair.name}: ERROR {exc}", file=sys.stderr)
        save_manifest(manifest_path, manifest)
    except Exception as exc:  # never block gateway boot
        print(f"vault-federate: FATAL {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
