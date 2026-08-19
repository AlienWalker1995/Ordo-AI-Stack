"""Two-way federation between Hermes memory files and the Obsidian vault.

Reconciles volume-side files (~/.hermes/SOUL.md, memories/*.md) with their
materialized vault notes using a content-hash manifest. Operator (vault)
wins conflicts; agent versions are preserved, never silently lost; SOUL.md
is never deleted. Stdlib only. Must never raise to its caller.

Spec: docs/superpowers/specs/2026-08-19-memory-stack-overhaul-design.md
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


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
