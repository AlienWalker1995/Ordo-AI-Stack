"""Replace mistranscribed words/phrases and inject punctuation in a ORDO_WORD_TIMINGS stream.

Two rule types share one widget — one rule per line:

  LITERAL multi-word rule:   pattern => replacement
       e.g.   soul striker => Solstryker
              bloog => blood

  REGEX rule (slash-delimited pattern):   /regex/ => replacement
       e.g.   /year (\\d+)/ => Year \\1:
              /house (\\w+)/ => House \\1
              /\\b(targaryen)\\b/ => Targaryen

For literal rules, matching is case-insensitive and ignores trailing punctuation
(.,!?;:"').

For regex rules, the pattern is matched against the space-joined word stream
case-insensitively. `\\1`, `\\2`, etc. expand to capture groups (use double
backslash in the widget if your file format escapes; ComfyUI accepts the
literal `\\1`). Matched word timings are merged into a single time span and
re-split evenly across the replacement's output tokens.

Lines starting with `#` are comments. Blank lines ignored.
"""

import logging
import re


logger = logging.getLogger("Ordo")


def _clean(w: str) -> str:
    return w.strip().strip(".,!?;:\"'").lower()


def _parse_rules(text: str) -> tuple[list[tuple[list[str], str]], list[tuple[re.Pattern, str]]]:
    """Return (literal_rules, regex_rules)."""
    literals: list[tuple[list[str], str]] = []
    regexes: list[tuple[re.Pattern, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        pat_part, rep_part = line.split("=>", 1)
        pat_part = pat_part.strip()
        rep_text = rep_part.strip()
        if not pat_part or not rep_text:
            continue
        if pat_part.startswith("/") and pat_part.endswith("/") and len(pat_part) >= 2:
            body = pat_part[1:-1]
            try:
                regexes.append((re.compile(body, re.IGNORECASE), rep_text))
            except re.error as e:
                logger.warning(f"[CaptionFixups] bad regex /{body}/: {e}")
            continue
        words = [_clean(w) for w in pat_part.split() if w.strip()]
        if words:
            literals.append((words, rep_text))
    # Longer literal patterns first
    literals.sort(key=lambda r: -len(r[0]))
    return literals, regexes


def _apply_literal(word_timings: list[dict], rules: list[tuple[list[str], str]]) -> tuple[list[dict], int]:
    if not rules:
        return list(word_timings), 0
    out: list[dict] = []
    hits = 0
    i = 0
    n = len(word_timings)
    while i < n:
        matched = False
        for pat_words, rep_text in rules:
            k = len(pat_words)
            if i + k > n:
                continue
            window = [_clean(word_timings[j]["word"]) for j in range(i, i + k)]
            if window == pat_words:
                _emit_replacement(out, word_timings, i, i + k, rep_text)
                i += k
                hits += 1
                matched = True
                break
        if not matched:
            out.append(dict(word_timings[i]))
            i += 1
    return out, hits


def _emit_replacement(out: list[dict], word_timings: list[dict], start: int, end: int, rep_text: str):
    """Append replacement tokens with timings spread evenly across the matched span."""
    rep_tokens = rep_text.split()
    if not rep_tokens:
        return
    t_start = word_timings[start]["start"]
    t_end = word_timings[end - 1]["end"]
    span = max(t_end - t_start, 1e-3)
    step = span / len(rep_tokens)
    seg_idx = word_timings[start].get("segment_idx", -1)
    for ti, tok in enumerate(rep_tokens):
        out.append({
            "word": tok,
            "start": t_start + ti * step,
            "end": t_start + (ti + 1) * step,
            "segment_idx": seg_idx,
        })


def _apply_regex(word_timings: list[dict], rules: list[tuple[re.Pattern, str]]) -> tuple[list[dict], int]:
    if not rules or not word_timings:
        return list(word_timings), 0

    # Build the space-joined stream and a char→word_idx map.
    joined_parts: list[str] = []
    word_starts: list[int] = []  # char offset where word i starts
    cur = 0
    for w in word_timings:
        word_starts.append(cur)
        joined_parts.append(w["word"])
        cur += len(w["word"]) + 1  # +1 for the joining space
    word_starts.append(cur)  # sentinel
    joined = " ".join(joined_parts)

    def char_to_word(char_pos: int, default: int) -> int:
        for i in range(len(word_starts) - 1):
            if word_starts[i] <= char_pos < word_starts[i + 1]:
                return i
        return default

    # Gather all matches from all regex rules, sorted by position; drop overlaps.
    candidates: list[tuple[int, int, re.Match, str]] = []
    for pat, rep in rules:
        for m in pat.finditer(joined):
            candidates.append((m.start(), m.end(), m, rep))
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))  # earliest, longest first

    accepted: list[tuple[int, int, re.Match, str]] = []
    last_end = -1
    for ms, me, m, rep in candidates:
        if ms < last_end:
            continue
        accepted.append((ms, me, m, rep))
        last_end = me

    if not accepted:
        return list(word_timings), 0

    result: list[dict] = []
    cursor = 0  # next word_idx to copy
    for ms, me, m, rep in accepted:
        start_word = char_to_word(ms, 0)
        end_word = char_to_word(me - 1, len(word_timings) - 1) + 1
        # Copy unchanged words before this match
        if start_word > cursor:
            result.extend(dict(w) for w in word_timings[cursor:start_word])
        # Expand backreferences (\1, \2 ...) — m.expand handles both \1 and $1 in Python? No, only \1.
        # Accept either by converting $1 -> \1 first.
        rep_text = re.sub(r"\$(\d+)", r"\\\1", rep)
        try:
            expanded = m.expand(rep_text)
        except re.error as e:
            logger.warning(f"[CaptionFixups] expand failed on rep={rep!r}: {e}")
            expanded = rep_text
        _emit_replacement(result, word_timings, start_word, end_word, expanded)
        cursor = end_word

    if cursor < len(word_timings):
        result.extend(dict(w) for w in word_timings[cursor:])

    return result, len(accepted)


class OrdoCaptionFixups:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "word_timings": ("ORDO_WORD_TIMINGS",),
                "replacements": ("STRING", {
                    "multiline": True,
                    "default": (
                        "# Literal multi-word: pattern => replacement (case-insensitive)\n"
                        "soul striker => Solstryker\n"
                        "sole striker => Solstryker\n"
                        "soul stryker => Solstryker\n"
                        "bloog => blood\n"
                        "\n"
                        "# Regex (slash-delimited): /pattern/ => replacement\n"
                        "# Use \\1, \\2, ... for capture groups.\n"
                        "/year (\\d+)/ => Year \\1:\n"
                        "/chapter (\\d+)/ => Chapter \\1:\n"
                    ),
                    "tooltip": (
                        "One rule per line. 'pattern => replacement' is literal "
                        "(case-insensitive multi-word). '/regex/ => replacement' is regex "
                        "(applied to the space-joined word stream)."
                    ),
                }),
            },
            "optional": {
                "bypass": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "True = skip replacements and pass word_timings through unchanged.",
                }),
            },
        }

    RETURN_TYPES = ("ORDO_WORD_TIMINGS",)
    RETURN_NAMES = ("word_timings",)
    FUNCTION = "fix"
    CATEGORY = "Ordo/LastMile"
    DESCRIPTION = "Fix Whisper mistranscriptions + inject punctuation via literal + regex rules."

    def fix(self, word_timings, replacements, bypass=False):
        if bypass:
            return (list(word_timings),)
        literals, regexes = _parse_rules(replacements)
        if not literals and not regexes:
            return (list(word_timings),)

        # Apply literals first (more specific), then regex (general patterns).
        out, lit_hits = _apply_literal(word_timings, literals)
        out, re_hits = _apply_regex(out, regexes)

        if lit_hits or re_hits:
            logger.info(
                f"[CaptionFixups] literal hits={lit_hits} regex hits={re_hits} "
                f"({len(word_timings)} input words -> {len(out)} output words)"
            )
        return (out,)
