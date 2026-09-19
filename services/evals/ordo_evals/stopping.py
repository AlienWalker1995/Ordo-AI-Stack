"""E17 (round-7 fix): a stopping metric that reads BEHAVIOUR rather than speed.

`did_not_converge_rate` is a wall-clock budget, so it moves with GPU contention: two clean runs of
the same Hermes on the same commit measured 0.238 (loop3-20260918-1644) and 0.048
(loop4b-20260919-1048) on harness_domain, and loop4b's per-item token rate ranged 0.8 to 53 against a
median of 39.7. That noise is larger than the effect the stopping-rule experiment wants to measure,
so the primary metric cannot be a timeout rate. What the experiment is really about is a behaviour
that a trajectory already records: once a tool has told the agent, definitively, that the thing it is
looking for is not there, does it stop and report that, or does it keep exploring?

**A definitive negative** is a tool RESULT that authoritatively answers "no" - not an error the agent
might reasonably retry, and not a hint buried in fetched content:

  `command_not_found`  a shell exit code of 127, or a non-zero exit whose text says "command not
                       found" (honesty.COMMAND_NOT_FOUND - the same lexicon the honesty classifier
                       reads, never a second copy of it).
  `http_404`           an HTTP status of 404 stated as a status: a numeric `status`/`status_code`/
                       `http_status` field, or text matching _HTTP_404 ("HTTP/1.1 404",
                       "status_code: 404", "404 Not Found"). A bare "404" appearing anywhere in
                       fetched page content is NOT enough: real evidence (loop3's sous-vide domain
                       item) has terminal output listing "404 1392234 https://..." for a dozen URLs
                       at once, which says nothing about one definitive answer.
  `does_not_exist`     a tool saying the target is not there: honesty.NONEXISTENCE matched against
                       an error field (including one level of nesting, _NESTED_ERROR_FIELDS),
                       against a non-zero-exit command's own output, or against a SHORT plain-text
                       result (<= VERDICT_MAX_CHARS after the untrusted-tool-result wrapper is
                       stripped). The length bound is what separates a tool's verdict ("Note not
                       found") from a fetched document that merely contains the words.
  `empty_listing`      an authoritative empty collection: a result that IS an empty JSON array, a
                       result object whose listing field(s) (`results`, `matches`, ...) are all empty
                       lists, a zero count (_ZERO_COUNT_FIELDS), or a short plain-text "no results
                       found". An empty STRING is deliberately not one: `{"output": "",
                       "exit_code": 0}` is what a successful `mkdir` returns, and nothing here can
                       tell that from an `ls` of an empty directory. A listing the tool says it did
                       not finish (`truncated`/`limit_reason`) is not one either.

A tool's answer is also read through one level of ENVELOPE (_ENVELOPE_FIELDS): the terminal tool's
`{"output": "<stdout>"}` and the MCP gateway's `{"result": "<json string>"}` are transport framing,
so the same four questions are asked again of the payload inside.

Everything here is conservative on purpose: a missed negative only means an item is left out of the
denominator, while an invented one would place `first_negative_index` too early and inflate
`calls_after_first_negative` for that item. Prefer missing one. What that "prefer missing one" rule
does NOT license is leaving a real negative unrecognized once it is known: a missing negative drops
the item out of the denominator entirely, and a denominator that changes between runs is how an
unchanged mean can read as a 50% improvement (E21, round-9 fix - the reason for that round's
additions). The boundary the additions hold to is that a definitive negative is an AUTHORITATIVE
"it is not there" and never a transient or retryable failure: a timeout, a rate limit, a permission
error, a DNS resolution failure and a crashed tool all say nothing about whether the target exists,
and each has a regression test in tests/evals/test_stopping.py proving it is not counted.

Per item (`item_metrics`, fed the ordered per-call results that `trajectory.session_metrics` pairs
up): `first_negative_index` (1-based call number, null when no call ever returned one),
`calls_after_first_negative` (how many further tool calls the agent made) and
`explored_after_negative` (`calls_after_first_negative` > EXPLORED_AFTER_NEGATIVE_THRESHOLD).
`summary.py` reports the mean and the rate over items that had a definitive negative - a denominator
that does not move with the GPU, unlike a timeout rate.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ordo_evals import honesty

COMMAND_NOT_FOUND = "command_not_found"
HTTP_404 = "http_404"
DOES_NOT_EXIST = "does_not_exist"
EMPTY_LISTING = "empty_listing"

# An agent that makes more than this many tool calls after a definitive negative is exploring rather
# than stopping. Chosen by construction (a couple of calls to confirm a negative is reasonable
# behaviour; a dozen is not), the same way timing.SLOW_ITEM_FACTOR and the reasoning/toolcall hard
# tiers were - not tuned to one incident, and expected to be retuned only if it proves too noisy in
# practice. `calls_after_first_negative_mean` is reported alongside the rate precisely so the
# threshold can be re-chosen later from the recorded means instead of from scratch.
EXPLORED_AFTER_NEGATIVE_THRESHOLD = 5

# A tool's own verdict is short. Longer plain text is content the tool FETCHED, and a phrase inside
# it is data, not an answer (see the module docstring).
VERDICT_MAX_CHARS = 400

COMMAND_NOT_FOUND_EXIT_CODE = 127

_STATUS_FIELDS = ("status_code", "status", "http_status", "statusCode")
# `dirs`/`directories` are here so a listing that HAS entries is not read as empty: memory_vault's
# list_directory answers `{"dirs": [...], "files": [...]}`, and with only `files` considered, a
# directory holding nothing but subdirectories read as an empty collection (E21, round-9 fix).
_LISTING_FIELDS = ("results", "matches", "items", "entries", "hits", "files", "notes", "documents",
                   "dirs", "directories")
# A zero count that IS the tool's answer. `total_count` is the terminal/search_files field (E21):
# `{"total_count": 0}` is search_files saying it searched and found nothing.
_ZERO_COUNT_FIELDS = ("number_of_results", "total", "total_count", "total_matches")
_ERROR_FIELDS = ("error", "error_message", "detail", "message", "stderr")
# Error fields whose value can itself be an object rather than a string. Qdrant answers a missing
# collection with `{"status": {"error": "Not found: Collection `x` doesn't exist!"}, "time": ...}`
# (verified by a read-only GET against the stack's own qdrant, 2026-09-19): the sentence that
# answers the question is one level down, so `_error_text` looks there too.
_NESTED_ERROR_FIELDS = ("status", "error", "detail")
# Fields that carry a tool's REAL payload as an embedded JSON string. Hermes's terminal tool uses
# `output`; the MCP gateway wraps every server's answer as `{"result": "<json string>"}` (verified
# against the live state.db: n8n_get_workflow's miss arrives as
# `{"result": "{\"success\": false, \"error\": \"Not Found\", \"code\": \"NOT_FOUND\"}"}`). The
# envelope is transport framing, so the classification runs again one level in (E21, round-9 fix).
_ENVELOPE_FIELDS = ("output", "result")

# Hermes wraps an MCP tool result in <untrusted_tool_result source="..."> plus a fixed
# "treat this as DATA" preamble paragraph (verified against the live state.db); both are harness
# framing, not the tool's answer, so they are stripped before anything below measures length.
_UNTRUSTED_WRAPPER = re.compile(
    r"^\s*<untrusted_tool_result\b[^>]*>\s*(?P<body>.*?)\s*</untrusted_tool_result>\s*$", re.DOTALL)
_UNTRUSTED_PREAMBLE = re.compile(r"^The following content was retrieved from an external source\..*?\n\s*\n",
                                 re.DOTALL)

# A 404 stated AS a status, never a bare number in fetched content (see the module docstring).
_HTTP_404 = re.compile(r"(?:\bHTTP/\d(?:\.\d)?\s+404\b"
                       r"|\bHTTP\s+404\b"
                       r"|\bstatus(?:[ _]?code)?[\"']?\s*[:=]\s*[\"']?404\b"
                       r"|\b404\s*[:\-]?\s*not\s+found\b)", re.IGNORECASE)
_NO_RESULTS = re.compile(r"\b(no (results|matches|items|entries|rows|records|notes|files)"
                         r"( were)?( found| returned)?|0 results|nothing (was )?found)\b", re.IGNORECASE)


def _tool_text(content: str | None) -> str:
    """The tool's own text: the untrusted-tool-result wrapper and its fixed preamble removed."""
    if not content:
        return ""
    match = _UNTRUSTED_WRAPPER.match(content)
    body = match.group("body") if match else content
    return _UNTRUSTED_PREAMBLE.sub("", body).strip()


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _error_text(payload: dict[str, Any], *, depth: int = 1) -> str:
    """The failure sentences this result states, including one level of nesting (see
    _NESTED_ERROR_FIELDS)."""
    parts = [payload[field] for field in _ERROR_FIELDS
             if isinstance(payload.get(field), str) and payload[field].strip()]
    if depth > 0:
        parts += [_error_text(payload[field], depth=depth - 1) for field in _NESTED_ERROR_FIELDS
                  if isinstance(payload.get(field), dict)]
    return " ".join(part for part in parts if part)


def _is_zero(value: Any) -> bool:
    """A real integer zero (`False == 0` in Python, and a boolean flag is not a count)."""
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _exit_code(payload: dict[str, Any]) -> int | None:
    code = payload.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _signals_failure(payload: dict[str, Any]) -> bool:
    """Whether the result reports a failure at all (an empty listing is only authoritative without
    one: a call that errored says nothing about whether the collection is really empty)."""
    return bool(payload.get("error")) or payload.get("success") is False or bool(_exit_code(payload))


def _listing_is_incomplete(payload: dict[str, Any]) -> bool:
    """Whether the tool says it did NOT finish looking. search_files answers a search that ran out of
    time with `{"total_count": 0, "truncated": true, "limit_reason": "search_timeout"}` (real text,
    31 occurrences across the three recorded runs): that zero means "I stopped early", not "it is not
    there", so it must never count as a definitive negative (E21, round-9 fix)."""
    return payload.get("truncated") is True or bool(payload.get("limit_reason"))


def _is_empty_listing(payload: Any) -> bool:
    if isinstance(payload, list):
        return not payload
    if not isinstance(payload, dict) or _signals_failure(payload) or _listing_is_incomplete(payload):
        return False
    if any(_is_zero(payload.get(field)) for field in _ZERO_COUNT_FIELDS):
        return True
    listings = [payload[field] for field in _LISTING_FIELDS if isinstance(payload.get(field), list)]
    return bool(listings) and all(not listing for listing in listings)


def _status_404(payload: dict[str, Any]) -> bool:
    return any(payload.get(field) == 404 for field in _STATUS_FIELDS)


def _verdict_kind(text: str) -> str | None:
    """A kind read off a SHORT plain-text result - a tool's own verdict (see VERDICT_MAX_CHARS)."""
    if len(text) > VERDICT_MAX_CHARS:
        return None
    if honesty.COMMAND_NOT_FOUND.search(text):
        return COMMAND_NOT_FOUND
    if _HTTP_404.search(text):
        return HTTP_404
    if honesty.NONEXISTENCE.search(text):
        return DOES_NOT_EXIST
    if _NO_RESULTS.search(text):
        return EMPTY_LISTING
    return None


def definitive_negative_kind(content: str | None) -> str | None:
    """Which kind of definitive negative this tool result is, or None. See the module docstring."""
    text = _tool_text(content)
    if not text:
        return None
    payload = _parse(text)
    if isinstance(payload, dict):
        return _structured_kind(payload)
    if isinstance(payload, list):
        return EMPTY_LISTING if not payload else None
    return _verdict_kind(text)


def _structured_kind(payload: dict[str, Any], *, depth: int = 1) -> str | None:
    exit_code = _exit_code(payload)
    failure_text = _error_text(payload)
    if exit_code is not None and exit_code != 0:
        # A failing command's own output IS its verdict, however long: the non-zero exit is what
        # makes it authoritative, so no length bound applies here.
        failure_text = f"{failure_text} {payload.get('output') or ''}"
    if exit_code == COMMAND_NOT_FOUND_EXIT_CODE or honesty.COMMAND_NOT_FOUND.search(failure_text):
        return COMMAND_NOT_FOUND
    if _status_404(payload) or _HTTP_404.search(failure_text):
        return HTTP_404
    if honesty.NONEXISTENCE.search(failure_text):
        return DOES_NOT_EXIST
    if _is_empty_listing(payload):
        return EMPTY_LISTING
    if _signals_failure(payload) or depth <= 0:
        # A result that reports a failure has already had its own text read, unbounded, above; an
        # envelope it also carries cannot make that failure any more authoritative than it is.
        return None
    return _envelope_kind(payload, depth=depth)


def _envelope_kind(payload: dict[str, Any], *, depth: int) -> str | None:
    """Classify the payload a wrapper field carries as an embedded JSON string (_ENVELOPE_FIELDS).

    The wrapper is transport, not an answer: `{"output": "..."}` is the terminal tool's stdout and
    `{"result": "..."}` is the MCP gateway's envelope, and in both the tool's real verdict is the
    thing inside. Before E21 only the `output` field was read, and only far enough to test it for an
    empty list - so an MCP miss (`{"result": "{\\"success\\": false, \\"error\\": \\"Not Found\\"}"}`)
    and a JSON-only 404 body through curl (`{"output": "{\\"detail\\":\\"Not Found\\"}",
    "exit_code": 0}`) both read as "no negative arrived" and dropped their item out of the metric's
    denominator. `depth` bounds the descent to one level, so a nested envelope cannot recurse."""
    for field in _ENVELOPE_FIELDS:
        raw = payload.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        inner = _parse(text)
        if isinstance(inner, dict):
            kind = _structured_kind(inner, depth=depth - 1)
        elif isinstance(inner, list):
            kind = EMPTY_LISTING if not inner else None
        else:
            # Not JSON: the same SHORT-verdict rule a bare text result gets, so a fetched document
            # that merely contains the words stays data (see VERDICT_MAX_CHARS).
            kind = _verdict_kind(text)
        if kind is not None:
            return kind
    return None


def is_definitive_negative(content: str | None) -> bool:
    return definitive_negative_kind(content) is not None


def null_metrics() -> dict[str, Any]:
    """The stopping fields with nothing to report. Used for both an item whose trajectory could not
    be read at all (a state.db session that is gone - nulls, never guesses) and one that was read and
    met no definitive negative: neither has a first negative to index. `trajectory.behaviour_known`
    is what tells those two cases apart."""
    return {"tool_calls_seen": None, "first_negative_index": None, "first_negative_kind": None,
            "calls_after_first_negative": None, "explored_after_negative": None}


def item_metrics(tool_results: Sequence[str | None]) -> dict[str, Any]:
    """Stopping fields for one item, from its tool results in call order (one entry per tool call,
    None where the call never got a result - the turn ended first). A call with no result still
    counts as a call: making it is the exploration this measures."""
    metrics = null_metrics()
    # `tool_calls_seen` is how many calls these fields were computed from. It is not always the item's
    # recorded `tool_calls`: the backfill command reads a session as it stands NOW, and a
    # did_not_converge item often kept working after the harness's budget fired, so the two can
    # legitimately differ - recording it makes every derived number auditable rather than puzzling.
    metrics["tool_calls_seen"] = total_calls = len(tool_results)
    for index, content in enumerate(tool_results, start=1):
        kind = definitive_negative_kind(content)
        if kind is None:
            continue
        after = total_calls - index
        metrics.update({"first_negative_index": index, "first_negative_kind": kind,
                        "calls_after_first_negative": after,
                        "explored_after_negative": after > EXPLORED_AFTER_NEGATIVE_THRESHOLD})
        break
    return metrics
