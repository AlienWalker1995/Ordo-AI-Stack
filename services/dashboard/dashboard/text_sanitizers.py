"""Helpers for cleaning model-emitted special token artifacts from plain-text fields."""

from __future__ import annotations

import re


def clean_gemma_special_tokens(text: str) -> str:
    """Replace Gemma special tokens (<|"|>, etc.) with literal characters."""
    if "<|" not in text:
        return text
    text = text.replace('<|"|>', '"')
    text = text.replace("<|'|>", "'")
    text = text.replace("<|`|>", "`")
    text = text.replace("<|\\n|>", "\n")
    return re.sub(r"<\|(.)\|>", r"\1", text)


def sanitize_workflow_id(workflow_id: str | None) -> str | None:
    """Normalize LLM-produced workflow ids before path resolution or DB storage."""
    if workflow_id is None:
        return None
    cleaned = clean_gemma_special_tokens(str(workflow_id)).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"', "`"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned or None
