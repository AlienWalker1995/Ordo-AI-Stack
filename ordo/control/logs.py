"""Container logs as every ops-controller logs route answers them: both streams, a cursor, a byte cap.

`docker logs` writes a container's stdout to its own stdout and the container's stderr to its own
stderr. Reading only the first left the dashboard's drawer empty for every service that logs to
stderr (llama.cpp, caddy and uvicorn among them), so the backend reads both with `--timestamps`
and `merge_streams` interleaves them by time.

The timestamps are also the cursor. A caller passes the last answer's `last_ts` back as `since`
and gets only the lines after it. Docker's `--since` is inclusive (a line stamped exactly `since`
comes back), so `payload` drops the lines at or before an absolute `since`: polling never repeats
a line. A duration (`10m`) is relative to now and is passed through as docker reads it.

A tail of 1000 lines of a chatty container can still be megabytes, so the body is capped in bytes
too (`BODY_MAX_BYTES`), keeping the newest lines, and `truncated` says when that happened.
"""
from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from datetime import UTC, datetime

TAIL_DEFAULT = 200
TAIL_MAX = 1000
BODY_MAX_BYTES = 256 * 1024

# RFC 3339 as docker prints it (`2026-10-17T04:10:21.044180797Z`) or a caller writes it (any
# fraction up to nanoseconds, `Z` or a numeric offset).
_RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})")
# A Go duration, the relative form `docker logs --since` accepts (`10m`, `1h30m`, `1.5h`).
_DURATION = re.compile(r"(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+")

# One parsed line: (its docker timestamp, its text without the timestamp).
LogLine = tuple[str, str]
# A comparable instant at nanosecond precision: (the whole second in UTC, nanoseconds).
_Instant = tuple[datetime, int]
_EARLIEST: _Instant = (datetime.min.replace(tzinfo=UTC), 0)


class LogQueryError(ValueError):
    """A `tail` or `since` the logs routes refuse (400)."""


@dataclass(frozen=True)
class LogQuery:
    tail: int
    since: str | None


def instant(timestamp: str) -> _Instant | None:
    """An RFC 3339 timestamp as a comparable instant, or None when it is not one. Python's
    datetime stops at microseconds, so the nanoseconds are kept beside the whole second."""
    found = _RFC3339.fullmatch(timestamp)
    if not found:
        return None
    seconds, fraction, zone = found.groups()
    try:
        whole = datetime.fromisoformat(seconds + ("+00:00" if zone == "Z" else zone))
    except ValueError:  # a shape that matches but is no date (month 13)
        return None
    return whole.astimezone(UTC), int((fraction or "").ljust(9, "0"))


def parse_query(query: dict[str, str]) -> LogQuery:
    """`tail` (an integer in 1..TAIL_MAX, default TAIL_DEFAULT) and `since` (RFC 3339 or a docker
    duration). Anything else raises LogQueryError: a `since` docker would read some other way
    (a unix timestamp, a date alone) is refused rather than guessed at."""
    raw_tail = query.get("tail")
    if raw_tail is None or raw_tail == "":
        tail = TAIL_DEFAULT
    else:
        try:
            tail = int(raw_tail)
        except ValueError:
            raise LogQueryError(f"tail must be an integer, got {raw_tail!r}") from None
        if not 1 <= tail <= TAIL_MAX:
            raise LogQueryError(f"tail must be between 1 and {TAIL_MAX}, got {tail}")
    since = query.get("since") or None
    if since is not None and instant(since) is None and not _DURATION.fullmatch(since):
        raise LogQueryError(f"since must be an RFC 3339 timestamp or a duration like '10m', got {since!r}")
    return LogQuery(tail=tail, since=since)


def _parse_stream(text: str) -> list[tuple[_Instant, str, str]]:
    """`docker logs --timestamps` output as (instant, timestamp, text) per line. A line without a
    timestamp (none is expected) sorts with the line before it."""
    parsed = []
    previous = _EARLIEST
    pieces = text.split("\n")
    if pieces and pieces[-1] == "":
        pieces.pop()  # the newline that ends the last line
    for line in pieces:
        timestamp, _, rest = line.partition(" ")
        when = instant(timestamp)
        if when is None:
            timestamp, rest, when = "", line, previous
        parsed.append((when, timestamp, rest))
        previous = when
    return parsed


def merge_streams(stdout: str, stderr: str) -> list[LogLine]:
    """Both streams of one `docker logs --timestamps` call as one list in time order. Each stream
    is already in order, so this is a merge, not a sort; a stdout line and a stderr line with the
    same timestamp keep stdout first."""
    merged = heapq.merge(_parse_stream(stdout), _parse_stream(stderr), key=lambda line: line[0])
    return [(timestamp, text) for _when, timestamp, text in merged]


def payload(lines: list[LogLine], query: LogQuery) -> dict:
    """The fields every logs route answers: `logs` (the text, timestamps removed, as the dashboard
    has always shown it), `tail`, `since`, `last_ts` (the newest returned line's timestamp, the
    next call's `since`; None when no line came back) and `truncated` (lines were dropped to stay
    under BODY_MAX_BYTES)."""
    since = instant(query.since) if query.since else None
    if since is not None:
        lines = [line for line in lines if (instant(line[0]) or _EARLIEST) > since]
    kept: list[LogLine] = []
    size = 0
    truncated = False
    for timestamp, text in reversed(lines):
        encoded = (text + "\n").encode("utf-8", errors="replace")
        if size + len(encoded) > BODY_MAX_BYTES:
            truncated = True
            if not kept:  # one line over the whole cap: keep its end, the newest bytes
                kept.append((timestamp, encoded[-BODY_MAX_BYTES:].decode("utf-8", errors="ignore")))
            break
        kept.append((timestamp, text + "\n"))
        size += len(encoded)
    kept.reverse()
    last_ts = next((timestamp for timestamp, _text in reversed(kept) if timestamp), None)
    return {"logs": "".join(text for _timestamp, text in kept), "tail": query.tail, "since": query.since,
            "last_ts": last_ts, "truncated": truncated}
