"""Redact secret-shaped substrings from anything the runner persists (E12, round-4 fix).

Iteration 2 saw an answer echo GitHub token prefixes (`ghp_...`, `github_pat_...`), which were
written to `items.jsonl`, `judge_queue.jsonl` and Langfuse verbatim - a real operator credential
stored in the clear because it happened to appear in a Hermes reply. `runner.run` calls
`redact_item`/`redact_queue_entry` on every item and judge-queue entry BEFORE any of it is written to
disk or posted anywhere (items.jsonl, judge_queue.jsonl, Langfuse dataset items and scores), so this
is the one place a leak like that is caught, not a per-suite patch repeated seven times.

This is a best-effort net on unstructured text, not a guarantee: a secret in a shape not covered below
still gets through. It leaves a `[REDACTED:<kind>]` marker in place of the removed text so a reader can
always tell something was there, rather than silently rewriting the record.
"""
from __future__ import annotations

import re
from typing import Any

_MARKER = "[REDACTED:{kind}]"

# Known secret-prefixed token shapes - the prefix itself is diagnostic enough that no surrounding
# context ("near a key-ish word") is needed before redacting.
_PREFIXED_TOKENS: list[tuple[str, re.Pattern[str]]] = [
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b")),
    ("langfuse_key", re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9-]{8,}\b")),
]

# A bare long hex/base64-ish run is often something entirely legitimate to persist as-is (a git
# commit SHA, a trace id, a content hash) - only redact one when it sits within a short window of a
# key-ish word, in either order ("token: <value>", "<value> is the api key"). The non-greedy `.{0,20}?`
# gap allows a short separator/connective ("=", ": ", " is the ") without reaching across unrelated
# text, and stops matching at a newline (default `.` behaviour) so this never spans separate lines.
_KEY_WORD = r"(?:api[_ -]?key|apikey|secret|token|password|passwd|bearer|auth)"
_LONG_RUN = r"[A-Za-z0-9+/_-]{24,}"
_GAP = r".{0,20}?"
_WORD_THEN_VALUE = re.compile(rf"(?i)\b{_KEY_WORD}\b{_GAP}({_LONG_RUN})")
_VALUE_THEN_WORD = re.compile(rf"(?i)({_LONG_RUN}){_GAP}\b{_KEY_WORD}\b")


def redact_secrets(text: str | None) -> str | None:
    """`text` with every secret-shaped substring replaced by a `[REDACTED:<kind>]` marker. `None` and
    non-strings pass through unchanged (callers hand this arbitrary field values from items.jsonl-
    shaped dicts, some of which are ints, bools or None)."""
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for kind, pattern in _PREFIXED_TOKENS:
        redacted = pattern.sub(_MARKER.format(kind=kind), redacted)

    def _replace_captured_run(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(1), _MARKER.format(kind="key_adjacent_value"))

    redacted = _WORD_THEN_VALUE.sub(_replace_captured_run, redacted)
    redacted = _VALUE_THEN_WORD.sub(_replace_captured_run, redacted)
    return redacted


def redact_value(value: Any) -> Any:
    """`redact_secrets` applied recursively through a JSON-shaped value (str/dict/list); every other
    type (int, bool, None, ...) passes through untouched."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_value(v) for key, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


# Item/queue-entry fields that can carry Hermes- or model-generated free text, as opposed to the
# structural fields (item_id, run_id, suite, subject, trace_id, scores, target, ...) that must never
# be touched - trace_id in particular is itself a 32-character hex string (ids.trace_id_for) that a
# blanket redaction over the whole record could otherwise mistake for a secret-shaped run.
_ITEM_TEXT_FIELDS = ("input", "output", "error")
_QUEUE_ENTRY_TEXT_FIELDS = ("input", "output")


def redact_item(item: dict[str, Any]) -> dict[str, Any]:
    """A copy of an items.jsonl-shaped record with every free-text field - `input`, `output`, `error`,
    and everything under `metadata` (recursively, including a recovered trajectory's
    last_assistant_message, E10) - redacted. Structural fields (item_id, run_id, suite, trace_id,
    scores, target, infra_error) are left untouched."""
    redacted = dict(item)
    for field in _ITEM_TEXT_FIELDS:
        if field in redacted:
            redacted[field] = redact_secrets(redacted[field])
    if "metadata" in redacted:
        redacted["metadata"] = redact_value(redacted["metadata"])
    return redacted


def redact_queue_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A copy of a judge_queue.jsonl-shaped entry with `input`/`output` (the ask and the reply judged)
    and `context` (e.g. harness_domain's `tools_used`) redacted. `rubric` is static template text,
    never operator or model output, so it is left alone."""
    redacted = dict(entry)
    for field in _QUEUE_ENTRY_TEXT_FIELDS:
        if field in redacted:
            redacted[field] = redact_secrets(redacted[field])
    if "context" in redacted:
        redacted["context"] = redact_value(redacted["context"])
    return redacted
