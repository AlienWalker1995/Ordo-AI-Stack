"""Short-answer normalization and exact-match scoring for model_reasoning.

The model is asked to end its reply with one line `ANSWER: <answer>`. Scoring is exact match after
normalization, never fuzzy: a reasoning item is right or wrong, and a missing ANSWER line is wrong.

Normalization (applied to both sides): case-fold, drop markdown emphasis/backticks and LaTeX
\boxed{} / $...$ wrappers, collapse whitespace, strip trailing sentence punctuation, and render a
pure number canonically (thousands separators removed, `12.50` -> `12.5`, `7.0` -> `7`). Numbers
also compare numerically, within the item's `tolerance` (default 0, i.e. exact).
"""
from __future__ import annotations

import math
import re

_ANSWER_LINE = re.compile(r"(?im)^[\s>*_`#-]*answer\s*[:=]\s*(.+?)\s*$")
_BOXED = re.compile(r"\\boxed\{([^{}]*)\}")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
BARE_ANSWER_MAX_CHARS = 40


def extract_final_answer(text: str | None) -> str | None:
    """The text after the LAST `ANSWER:` line, or None when the reply has no such line."""
    if not text:
        return None
    matches = _ANSWER_LINE.findall(text)
    if not matches:
        return None
    return matches[-1].strip()


def answer_for_scoring(text: str | None) -> str | None:
    """The answer to score: the ANSWER line when present; otherwise the whole reply when it is a single
    short line (a bare `50`), because model_reasoning measures reasoning and reports protocol compliance
    separately as format_rate. A longer reply without an ANSWER line has no answer."""
    answer = extract_final_answer(text)
    if answer is not None:
        return answer
    stripped = (text or "").strip()
    if stripped and "\n" not in stripped and len(stripped) <= BARE_ANSWER_MAX_CHARS:
        return stripped
    return None


def _as_number(value: str) -> float | None:
    candidate = _THOUSANDS.sub("", value.strip())
    if _NUMBER.match(candidate):
        try:
            number = float(candidate)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def normalize_answer(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value)
    boxed = _BOXED.search(text)
    if boxed:
        text = boxed.group(1)
    text = text.strip().strip("$").strip()
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = text.rstrip(".;!").strip()
    number = _as_number(text)
    if number is not None:
        if number.is_integer():
            return str(int(number))
        return repr(number)
    return text


def answers_match(expected: str, got: str | None, *, aliases: list[str] | None = None,
                  tolerance: float = 0.0) -> bool:
    """True when `got` equals `expected` (or an alias) after normalization, or numerically within
    `tolerance` when both sides are pure numbers."""
    if got is None:
        return False
    normalized_got = normalize_answer(got)
    if not normalized_got:
        return False
    candidates = [expected, *(aliases or [])]
    for candidate in candidates:
        normalized_expected = normalize_answer(candidate)
        if normalized_got == normalized_expected:
            return True
        expected_number = _as_number(normalized_expected)
        got_number = _as_number(normalized_got)
        if expected_number is not None and got_number is not None:
            if abs(expected_number - got_number) <= max(tolerance, 1e-9):
                return True
    return False
