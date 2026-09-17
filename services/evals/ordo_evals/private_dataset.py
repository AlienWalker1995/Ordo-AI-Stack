"""Build the PRIVATE model_domain dataset from real operator asks in Hermes's state.db.

The output lives only under /results/datasets (outside git). This module never logs message
content: callers get counts, and the CLI prints counts only.

Selection, in order:
  1. user messages from Discord sessions (`sessions.source = 'discord'`, `messages.role = 'user'`);
  2. the gateway's leading speaker tag (`[name] `, one token in square brackets) is stripped, and
     any text that still starts with `[` is dropped (context-compaction summaries, channel-history
     preambles and similar injected annotations are not asks);
  3. SELF-CONTAINED QUESTIONS only: 15-500 characters, at most 3 lines, reads as a question or a
     direct request, and has no anaphora that points at earlier conversation ("that", "it", "the
     above", "try again", ...), no Discord mentions and no links (a question about a link or a
     previous message cannot be answered from the text alone);
  4. de-duplicated on a normalized form (case-folded, punctuation and whitespace collapsed);
  5. a seeded sample of `n`, drawn from candidates sorted by content hash so the sample depends on
     (candidates, seed) only, never on database row order.
"""
from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path
from typing import Any

from ordo_evals.trajectory import connect_readonly

MIN_CHARS = 15
MAX_CHARS = 500
MAX_LINES = 3

_SPEAKER_TAG = re.compile(r"^\[[^\s\[\]]{1,40}\]\s*")
_QUESTION_START = re.compile(
    r"^(what|what's|whats|how|why|when|where|which|who|whom|whose|can|could|would|should|is|are|am|"
    r"does|do|did|will|explain|compare|describe|tell me|give me|list|write|summarize|summarise|"
    r"recommend|suggest|help me|show me|calculate|convert|define|translate)\b",
    re.IGNORECASE,
)
_CONTEXT_START = re.compile(
    r"^(it|its|it's|that|this|those|these|they|them|he|she|yes|yeah|yep|no|nope|ok|okay|also|and|but|so|"
    r"again|continue|same|thanks|thank you|lol|hmm|now|then|still|ugh|wait)\b",
    re.IGNORECASE,
)
_CONTEXT_ANYWHERE = re.compile(
    r"\b(above|previous|earlier|that one|you just|the last one|as i said|like before|try again|do it again|"
    r"what you said|the same thing|this one|that file|this file|the screenshot|attached)\b",
    re.IGNORECASE,
)
_MENTION = re.compile(r"<[@#][!&]?\d+>")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)


def clean_message(text: str | None) -> str | None:
    """The ask with the speaker tag stripped, or None when the message is not an ask."""
    if not text:
        return None
    stripped = _SPEAKER_TAG.sub("", text.strip(), count=1).strip()
    if not stripped or stripped.startswith("["):
        return None
    return stripped


def is_self_contained_question(text: str) -> bool:
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if text.count("\n") >= MAX_LINES:
        return False
    if _MENTION.search(text) or _URL.search(text):
        return False
    if _CONTEXT_START.match(text) or _CONTEXT_ANYWHERE.search(text):
        return False
    return "?" in text or bool(_QUESTION_START.match(text))


def normalized_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.casefold())).strip()


def _content_hash(text: str) -> str:
    return hashlib.sha256(normalized_key(text).encode("utf-8")).hexdigest()


def candidate_asks(db_path: str | Path) -> list[str]:
    """Every distinct self-contained Discord ask, sorted by content hash."""
    connection = connect_readonly(db_path)
    try:
        rows = connection.execute(
            "SELECT m.content FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.source = 'discord' AND m.role = 'user'").fetchall()
    finally:
        connection.close()
    by_key: dict[str, str] = {}
    for row in rows:
        ask = clean_message(row["content"])
        if ask is None or not is_self_contained_question(ask):
            continue
        by_key.setdefault(normalized_key(ask), ask)
    return sorted(by_key.values(), key=_content_hash)


def build_private_dataset(db_path: str | Path, n: int, seed: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(items, stats). Items are {"id", "input", "source"}; stats hold counts only, never content."""
    if n <= 0:
        raise ValueError("n must be positive")
    candidates = candidate_asks(db_path)
    chosen = random.Random(seed).sample(candidates, k=min(n, len(candidates)))
    items = [{"id": f"pd-{_content_hash(ask)[:12]}", "input": ask, "source": "hermes-state:discord"}
             for ask in chosen]
    stats = {"candidates": len(candidates), "selected": len(items), "requested": n, "seed": seed}
    return items, stats
