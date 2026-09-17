"""Build the PRIVATE model_domain dataset from real operator asks in Hermes's state.db.

The output lives only under /results/datasets (outside git). This module never logs message
content: callers get counts, and the CLI prints counts only.

Selection, in order:
  1. user messages from Discord sessions (`sessions.source = 'discord'`, `messages.role = 'user'`);
  2. the gateway's leading speaker tag (`[name] `, one token in square brackets) is stripped, and
     any text that still starts with `[` is dropped (context-compaction summaries, channel-history
     preambles and similar injected annotations are not asks);
  3. SELF-CONTAINED QUESTIONS only: 15-500 characters, at least 4 words, at most 3 lines, reads as a
     question or a direct request, and has no anaphora that points at earlier conversation ("that",
     "it", "the above", "what about", "try again", ...), no Discord mentions and no links (a question
     about a link or a previous message cannot be answered from the text alone). Also excluded: asks
     that direct a tool or service a bare model has none of (search, qdrant, vault, n8n, workflow,
     docker, cron, ...) or that possessively name the operator's own stored data ("my vault", "our
     workflows") - see is_tool_directed;
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
MIN_WORDS = 4

_SPEAKER_TAG = re.compile(r"^\[[^\s\[\]]{1,40}\]\s*")
_QUESTION_START = re.compile(
    r"^(what|what's|whats|how|why|when|where|which|who|whom|whose|can|could|would|should|is|are|am|"
    r"does|do|did|will|explain|compare|describe|tell me|give me|list|write|summarize|summarise|"
    r"recommend|suggest|help me|show me|calculate|convert|define|translate)\b",
    re.IGNORECASE,
)
# Leading words that mark a message as a FOLLOW-UP on a prior turn rather than a self-contained ask.
# "what about" is checked ahead of the generic "what" question-start match (see is_self_contained_question).
_CONTEXT_START = re.compile(
    r"^(what about|it|its|it's|that|this one|this|those|these|they|them|he|she|yes|yeah|yep|no|nope|ok|"
    r"okay|also|and|but|so|again|continue|same|thanks|thank you|lol|hmm|now|then|still|ugh|wait)\b",
    re.IGNORECASE,
)
_CONTEXT_ANYWHERE = re.compile(
    r"\b(above|previous|earlier|that one|you just|the last one|as i said|like before|try again|do it again|"
    r"what you said|the same thing|the same one|the other one|this one|that file|this file|the screenshot|"
    r"attached)\b",
    re.IGNORECASE,
)
_MENTION = re.compile(r"<[@#][!&]?\d+>")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)

# A raw model with no tools, no MCP servers and no memory of the operator's stored data cannot
# fairly answer an ask that directs a tool/service or reaches for the operator's own data.
_TOOL_OR_SERVICE = re.compile(
    r"\b(search|searching|qdrant|vault|note|notes|n8n|workflow|workflows|comfy|comfyui|render|"
    r"rendering|docker|container|containers|cron|run|running)\b",
    re.IGNORECASE,
)
_MY_OUR_DATA_NOUN = re.compile(
    r"\b(my|our)\b(?:\s+\w+){0,3}\s+(vault|collection|collections|notes?|workflow|workflows|history|"
    r"data|files?|documents?|docs?|database|memory|sessions?|logs?|config|stack)\b",
    re.IGNORECASE,
)


def is_tool_directed(text: str) -> bool:
    """True when the ask directs a tool or service a bare, memoryless model has none of (search,
    qdrant, vault, n8n, workflow, docker, cron, ...), or possessively names the operator's own stored
    data ("my vault", "our workflows"). Either way a model_domain item built from this ask would not
    be answerable fairly by the bare-model subject."""
    return bool(_TOOL_OR_SERVICE.search(text) or _MY_OUR_DATA_NOUN.search(text))


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
    if len(text.split()) < MIN_WORDS:
        return False
    if text.count("\n") >= MAX_LINES:
        return False
    if _MENTION.search(text) or _URL.search(text):
        return False
    if _CONTEXT_START.match(text) or _CONTEXT_ANYWHERE.search(text):
        return False
    if is_tool_directed(text):
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
