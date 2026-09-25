#!/usr/bin/env python3
"""Delete Langfuse traces older than LANGFUSE_RETENTION_DAYS, through Langfuse's public API only.

Why this exists: self-hosted Langfuse ships data retention as an Enterprise feature (it needs
LANGFUSE_EE_LICENSE_KEY), and the maintainers discourage hand-editing ClickHouse TTLs. The public
API of the running v4.36 server does expose what retention needs (verified against its
/generated/api/openapi.yml and a live backdated-trace round trip on 2026-09-15):

  * enumerate: GET /api/public/v2/observations?toStartTime=<cutoff>&fields=core, cursor-paginated,
    at most 1000 rows per page. This is the v4 read path; GET /api/public/traces answers 404 in the
    default `events_only` write mode.
  * delete:    DELETE /api/public/traces with {"traceIds": [...]}, at most 1000 ids per request
    (the server's own request-body limit). Deleting a trace removes its observations and scores.

A run collects the distinct trace ids of every observation that started before the cutoff, then
deletes them in batches. It is idempotent: an id deleted twice is a no-op, and a run that stops
early (API error, per-run cap) is finished by the next one because the cutoff is recomputed. A
trace is judged by its OLDEST observation, so a long-lived trace that straddles the cutoff is
deleted whole.

Modes (stdlib only, so it runs on a plain pinned python image and the tests need nothing):
  --once         one run, then exit: 0 done, 1 API error, 2 bad configuration
  --daemon       the `langfuse-retention` compose service: one run shortly after start, then one
                 every day at LANGFUSE_RETENTION_RUN_AT (UTC). A failed run is logged and recorded
                 in the status file (the container goes unhealthy) and retried at the next slot.
  --healthcheck  exit 1 while the most recent recorded run is a failure, else 0
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

# Mounted beside this script (/app/secret_env.py); the canonical copy is ordo/secret_env.py.
from secret_env import SecretFileError, read_secret

# Server-side ceilings (Langfuse v4.36): the v2/observations `limit` maximum and the DELETE /traces
# id maximum.
OBSERVATIONS_PAGE_LIMIT = 1000
DELETE_BATCH_SIZE = 1000
# Upper bound on traces one run deletes, so a first run against a large backlog stays a bounded
# amount of work. Whatever is left over is picked up by the next run.
MAX_TRACES_PER_RUN = 100_000
# Transient failures (connection refused, timeouts, 5xx) are retried with this backoff before the
# run is declared failed. A 4xx is a contract or credential problem and fails at once.
RETRY_BACKOFF_SECONDS = (5.0, 15.0)
DEFAULT_BASE_URL = "http://langfuse-web:3000"
DEFAULT_RUN_AT = "04:45"
# The daemon's first run waits this long, so a stack coming up does not race langfuse-web's boot.
STARTUP_DELAY_SECONDS = 300
# A daily slot closer than this counts as already served (see seconds_until_next_run).
MIN_SLEEP_SECONDS = 60
STATUS_FILE = Path("/tmp/langfuse-retention-status.json")


class ConfigError(ValueError):
    """The environment cannot describe a valid run (exit 2)."""


class ApiError(RuntimeError):
    """The Langfuse API refused or failed a request (exit 1)."""


class LangfuseApi(Protocol):
    def observations_before(self, cutoff_iso: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]: ...
    def delete_traces(self, trace_ids: list[str]) -> None: ...


class HttpLangfuseApi:
    """The real public-API client: HTTP Basic auth with the project key pair."""

    def __init__(self, base_url: str, public_key: str, secret_key: str, timeout: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self.timeout = timeout
        self._sleep = sleep

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        endpoint = f"{method} {path.split('?', 1)[0]}"
        attempts = len(RETRY_BACKOFF_SECONDS) + 1
        last_error = ""
        for attempt in range(attempts):
            request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
                "Authorization": self._auth_header,
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode()
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                last_error = f"{endpoint} returned HTTP {e.code}: {detail}"
                if e.code < 500:
                    raise ApiError(last_error) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = f"{endpoint} failed: {e}"
            else:
                try:
                    return json.loads(raw) if raw else {}
                except ValueError as e:
                    raise ApiError(f"{endpoint} returned a non-JSON body: {raw[:200]!r}") from e
            if attempt < attempts - 1:
                self._sleep(RETRY_BACKOFF_SECONDS[attempt])
        raise ApiError(last_error)

    def observations_before(self, cutoff_iso: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        params = {"toStartTime": cutoff_iso, "fields": "core", "limit": str(OBSERVATIONS_PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        page = self._request("GET", "/api/public/v2/observations?" + urllib.parse.urlencode(params))
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ApiError(f"GET /api/public/v2/observations returned an unexpected shape: {str(page)[:200]}")
        next_cursor = (page.get("meta") or {}).get("cursor") or None
        return [row for row in page["data"] if isinstance(row, dict)], next_cursor

    def delete_traces(self, trace_ids: list[str]) -> None:
        self._request("DELETE", "/api/public/traces", {"traceIds": trace_ids})


@dataclasses.dataclass(frozen=True)
class Settings:
    base_url: str
    public_key: str
    secret_key: str
    retention_days: int
    run_at: tuple[int, int]


@dataclasses.dataclass(frozen=True)
class RunReport:
    cutoff: str
    traces_found: int
    traces_deleted: int
    delete_requests: int
    capped: bool


def parse_run_at(value: str) -> tuple[int, int]:
    """`"04:45"` -> (4, 45). Anything that is not a valid 24-hour HH:MM is a ConfigError."""
    try:
        hour_text, minute_text = value.strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ConfigError(f"LANGFUSE_RETENTION_RUN_AT must be HH:MM in UTC (got {value!r})") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"LANGFUSE_RETENTION_RUN_AT must be HH:MM in UTC (got {value!r})")
    return hour, minute


def settings_from_env(env: Mapping[str, str]) -> Settings:
    try:
        # Each from the file <NAME>_FILE points at (the rendered delivery), else the env var.
        public_key = read_secret("LANGFUSE_PUBLIC_KEY", env)
        secret_key = read_secret("LANGFUSE_SECRET_KEY", env)
    except SecretFileError as e:
        raise ConfigError(str(e)) from None
    if not public_key or not secret_key:
        raise ConfigError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set (the secret store)")
    raw_days = str(env.get("LANGFUSE_RETENTION_DAYS", "") or "").strip()
    try:
        retention_days = int(raw_days)
    except ValueError:
        raise ConfigError(f"LANGFUSE_RETENTION_DAYS must be a whole number of days (got {raw_days!r})") from None
    if retention_days < 1:
        raise ConfigError(f"LANGFUSE_RETENTION_DAYS must be at least 1 (got {retention_days})")
    return Settings(
        base_url=str(env.get("LANGFUSE_BASE_URL", "") or DEFAULT_BASE_URL),
        public_key=public_key,
        secret_key=secret_key,
        retention_days=retention_days,
        run_at=parse_run_at(str(env.get("LANGFUSE_RETENTION_RUN_AT", "") or DEFAULT_RUN_AT)),
    )


def cutoff_for(now: datetime, retention_days: int) -> str:
    """The ISO-8601 UTC instant `retention_days` before `now`, in the `...Z` form Langfuse accepts."""
    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff.microsecond // 1000:03d}Z"


def expired_trace_ids(api: LangfuseApi, cutoff_iso: str,
                      max_traces: int = MAX_TRACES_PER_RUN) -> tuple[list[str], bool]:
    """Distinct trace ids (first-seen order) of every observation that started before the cutoff.

    Returns (ids, capped). `capped` is True when the walk stopped at `max_traces` with more left.
    """
    ids: list[str] = []
    seen: set[str] = set()
    cursor: str | None = None
    while True:
        rows, cursor = api.observations_before(cutoff_iso, cursor)
        for row in rows:
            trace_id = str(row.get("traceId") or "")
            if not trace_id or trace_id in seen:
                continue
            if len(ids) >= max_traces:
                return ids, True
            seen.add(trace_id)
            ids.append(trace_id)
        if not cursor or not rows:
            return ids, False


def run_once(api: LangfuseApi, retention_days: int, now: datetime,
             log: Callable[[str], None] = print, max_traces: int = MAX_TRACES_PER_RUN) -> RunReport:
    cutoff = cutoff_for(now, retention_days)
    log(f"langfuse-retention: deleting traces with observations older than {cutoff} "
        f"(retention {retention_days} days)")
    ids, capped = expired_trace_ids(api, cutoff, max_traces)
    log(f"langfuse-retention: found {len(ids)} expired trace(s)" + (" (per-run cap reached)" if capped else ""))
    deleted = 0
    requests = 0
    for start in range(0, len(ids), DELETE_BATCH_SIZE):
        batch = ids[start:start + DELETE_BATCH_SIZE]
        api.delete_traces(batch)
        requests += 1
        deleted += len(batch)
        log(f"langfuse-retention: delete request {requests}: {len(batch)} trace(s), {deleted}/{len(ids)} done")
    log(f"langfuse-retention: finished, {deleted} trace(s) deleted in {requests} delete request(s)")
    return RunReport(cutoff=cutoff, traces_found=len(ids), traces_deleted=deleted,
                     delete_requests=requests, capped=capped)


def seconds_until_next_run(now: datetime, run_at: tuple[int, int]) -> float:
    """Seconds from `now` to the next HH:MM UTC, skipping a slot that is already effectively here.

    A plain "strictly in the future" test double-fires: `time.sleep` wakes a fraction of a second
    EARLY, so a run that starts at 04:44:59.5 computes half a second to 04:45:00 and runs the job
    twice (observed live 2026-09-16). Anything inside MIN_SLEEP_SECONDS of the target counts as
    that slot having been served, so the wait is always a real day.
    """
    now_utc = now.astimezone(UTC)
    target = now_utc.replace(hour=run_at[0], minute=run_at[1], second=0, microsecond=0)
    while (target - now_utc).total_seconds() < MIN_SLEEP_SECONDS:
        target += timedelta(days=1)
    return (target - now_utc).total_seconds()


def write_status(path: Path, ok: bool, detail: Mapping[str, Any]) -> None:
    payload = {"ok": ok, "finished_at": datetime.now(UTC).isoformat(), **detail}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def healthcheck(path: Path) -> int:
    """0 until a run has failed; 1 while the most recent recorded run is a failure."""
    if not path.exists():
        return 0
    try:
        return 0 if json.loads(path.read_text(encoding="utf-8")).get("ok") else 1
    except (OSError, ValueError):
        return 1


def run_and_record(api: LangfuseApi, retention_days: int, status_path: Path,
                   now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    """One run with its outcome written to the status file. 0 on success, 1 on an API error."""
    try:
        report = run_once(api, retention_days, now())
    except ApiError as e:
        print(f"langfuse-retention: FAILED: {e}", file=sys.stderr, flush=True)
        write_status(status_path, False, {"error": str(e)})
        return 1
    write_status(status_path, True, dataclasses.asdict(report))
    return 0


def daemon(api: LangfuseApi, settings: Settings, status_path: Path,
           sleep: Callable[[float], None] = time.sleep, max_runs: int | None = None) -> None:
    """Run STARTUP_DELAY_SECONDS after start, then daily at settings.run_at. `max_runs` is for tests."""
    hour, minute = settings.run_at
    print(f"langfuse-retention: daemon started (retention {settings.retention_days} days, daily at "
          f"{hour:02d}:{minute:02d} UTC, first run in {STARTUP_DELAY_SECONDS}s)", flush=True)
    sleep(STARTUP_DELAY_SECONDS)
    runs = 0
    while max_runs is None or runs < max_runs:
        run_and_record(api, settings.retention_days, status_path)
        runs += 1
        wait = seconds_until_next_run(datetime.now(UTC), settings.run_at)
        print(f"langfuse-retention: next run in {int(wait)}s", flush=True)
        sleep(wait)


def main(argv: list[str], env: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(description="Delete Langfuse traces older than LANGFUSE_RETENTION_DAYS.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run once and exit")
    mode.add_argument("--daemon", action="store_true", help="run shortly after start, then daily")
    mode.add_argument("--healthcheck", action="store_true", help="exit 1 if the last recorded run failed")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return healthcheck(STATUS_FILE)
    try:
        settings = settings_from_env(env)
    except ConfigError as e:
        print(f"langfuse-retention: {e}", file=sys.stderr, flush=True)
        return 2
    api = HttpLangfuseApi(settings.base_url, settings.public_key, settings.secret_key)
    if args.once:
        return run_and_record(api, settings.retention_days, STATUS_FILE)
    daemon(api, settings, STATUS_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
