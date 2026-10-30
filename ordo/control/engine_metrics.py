"""Per-engine model performance: each engine read its own way, exported as one metric family.

Every served model whose engine declares a metrics format (ordo/render/engines.py) is read here and
reported as `ordo_engine_*{engine, model, device, kind}`, so Grafana and the dashboard compare a GPU
engine, a CPU engine and any engine a plugin adds with the same definitions:

  prompt_tokens_processed  prompt tokens the engine computed (prefix-cache hits excluded)
  prompt_tokens_cached     prompt tokens reused from the engine's cache
  output_tokens            tokens generated
  prompt_seconds           time spent computing prompts
  output_seconds           time spent generating
  queue_seconds            time requests waited for the engine (only where the engine reports it)
  requests_running / requests_waiting   gauges, as of the last reading

Readers:
  llamacpp     the llama.cpp server's own /metrics (`--metrics`).
  ninfer-log   NInfer's request log, read from the container with `docker logs --since`. This is a
               TEMPORARY reader: NInfer has no metrics endpoint yet, and the durable fix is one
               upstream. Its counters start at zero when ops-controller starts (Prometheus' rate()
               handles the reset).

A reader that fails reports the engine as down (`ordo_engine_up 0`) and leaves its counters out,
never zeroed, so a broken reader cannot look like an idle engine.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

COUNTERS = ("prompt_tokens_processed", "prompt_tokens_cached", "output_tokens",
            "prompt_seconds", "output_seconds", "queue_seconds")
GAUGES = ("requests_running", "requests_waiting")

# llama.cpp metric name -> ordo name. Its prompt_tokens_total already excludes cached tokens.
_LLAMACPP_COUNTERS = {
    "llamacpp:prompt_tokens_total": "prompt_tokens_processed",
    "llamacpp:prompt_tokens_cached_total": "prompt_tokens_cached",
    "llamacpp:tokens_predicted_total": "output_tokens",
    "llamacpp:prompt_seconds_total": "prompt_seconds",
    "llamacpp:tokens_predicted_seconds_total": "output_seconds",
}
_LLAMACPP_GAUGES = {
    "llamacpp:requests_processing": "requests_running",
    "llamacpp:requests_deferred": "requests_waiting",
}

# Lines the NInfer log reader reads (one finished request, and the 5-second throughput summary):
#   req#54 done | openai-chat | output limit | prompt 1,722 | output 3,000 | cache 0 (0.0%) | TTFT 39.5s | total 59.7s | queue 39.1s | ...
#   throughput | 5.0s | prefill ... | running 1 (decode-ready 1) | waiting 7 | ...
_NINFER_DONE = re.compile(r"\breq#\d+ done \|")
_NINFER_THROUGHPUT = re.compile(r"\bthroughput \|")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|us|µs|h|m|s)\b")
_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6}

# The most log lines read per scrape. NInfer writes about one line a second, so a 15-second scrape
# reads a few dozen; this bounds a scrape after a long gap rather than reading the whole log.
NINFER_LOG_TAIL = 5000


@dataclasses.dataclass
class EngineSample:
    """One engine's reading for one scrape. `counters`/`gauges` are empty when `up` is False."""
    labels: dict[str, str]
    up: bool
    counters: dict[str, float] = dataclasses.field(default_factory=dict)
    gauges: dict[str, float] = dataclasses.field(default_factory=dict)


def parse_llamacpp(text: str) -> tuple[dict[str, float], dict[str, float]]:
    """(counters, gauges) from a llama.cpp /metrics body. ValueError when it has no llama.cpp series."""
    counters: dict[str, float] = {}
    gauges: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "{" in line:
            continue
        name, _, value = line.partition(" ")
        if name in _LLAMACPP_COUNTERS:
            counters[_LLAMACPP_COUNTERS[name]] = float(value)
        elif name in _LLAMACPP_GAUGES:
            gauges[_LLAMACPP_GAUGES[name]] = float(value)
    if not counters:
        raise ValueError("no llama.cpp counters in the metrics body (is the server run with --metrics?)")
    return counters, gauges


def parse_duration(text: str) -> float:
    """Seconds in a NInfer duration: "39.5s", "1m 4.1s", "665 us", "1h 2m"."""
    parts = _DURATION_PART.findall(text)
    if not parts:
        raise ValueError(f"not a duration: {text!r}")
    return sum(float(number) * _DURATION_UNITS[unit] for number, unit in parts)


def _fields(line: str) -> dict[str, str]:
    """Numeric `key value` fields of a pipe-separated NInfer line, keyed by their first word. Only
    values that start with a digit count: the stop reason "output limit" shares its first word with
    the "output 3,000" token count."""
    out: dict[str, str] = {}
    for field in line.split(" | "):
        key, _, value = field.strip().partition(" ")
        if value[:1].isdigit():
            out.setdefault(key, value)
    return out


def _count(value: str) -> float:
    """"1,722" or "0 (0.0%)" -> the leading count."""
    return float(value.split(" ", 1)[0].replace(",", ""))


class NinferLogReader:
    """Running totals over NInfer's request log, fed the lines since the last read."""

    def __init__(self, started_at: float) -> None:
        self.counters = dict.fromkeys(COUNTERS, 0.0)
        self.gauges: dict[str, float] = {}
        # Where the next read starts: a Unix time first, then the last line's RFC 3339 timestamp.
        # `docker logs --since` is inclusive, so lines at exactly that instant are matched against
        # the ones already counted.
        self.since = str(int(started_at))
        self._at_since: set[str] = set()

    def consume(self, lines: Iterable[tuple[str, str]]) -> None:
        last_ts = None
        at_last: set[str] = set()
        saw_throughput = False
        for ts, text in lines:
            if ts == self.since and text in self._at_since:
                continue   # already counted by the previous read
            if ts != last_ts:
                last_ts, at_last = ts, set()
            at_last.add(text)
            if _NINFER_DONE.search(text):
                self._count_request(text)
            elif _NINFER_THROUGHPUT.search(text):
                saw_throughput = True
                fields = _fields(text)
                if "running" in fields:
                    self.gauges["requests_running"] = _count(fields["running"])
                self.gauges["requests_waiting"] = _count(fields["waiting"]) if "waiting" in fields else 0.0
        if not saw_throughput:
            # NInfer prints its throughput summary only while it is working: none since the last
            # read means nothing is running or waiting.
            self.gauges = {"requests_running": 0.0, "requests_waiting": 0.0}
        if last_ts is not None:
            self._at_since = at_last | (self._at_since if last_ts == self.since else set())
            self.since = last_ts

    def _count_request(self, text: str) -> None:
        fields = _fields(text)
        try:
            prompt, output, cached = _count(fields["prompt"]), _count(fields["output"]), _count(fields["cache"])
            ttft, total = parse_duration(fields["TTFT"]), parse_duration(fields["total"])
            # NInfer leaves the field out when the request did not wait.
            queue = parse_duration(fields["queue"]) if "queue" in fields else 0.0
        except (KeyError, ValueError) as e:
            logger.warning("engine metrics: unreadable NInfer request line (%s): %s", e, text[:200])
            return
        self.counters["prompt_tokens_processed"] += max(prompt - cached, 0.0)
        self.counters["prompt_tokens_cached"] += cached
        self.counters["output_tokens"] += output
        # TTFT includes the wait for the engine; a request cancelled while queued has TTFT < queue.
        self.counters["prompt_seconds"] += max(ttft - queue, 0.0)
        self.counters["output_seconds"] += max(total - ttft, 0.0)
        self.counters["queue_seconds"] += queue


class EngineCollector:
    """Reads every measured engine once per scrape.

    `engines()` is the registry's served-model records (ordo/render/served_models.py), re-read at
    most every `refresh_seconds` because it renders the stack. `http_get(url)` returns a body or
    raises; `read_logs(service, since, tail)` returns (timestamp, text) lines or raises."""

    def __init__(self, engines: Callable[[], Mapping[str, Mapping[str, Any]]],
                 http_get: Callable[[str], str],
                 read_logs: Callable[[str, str, int], Sequence[tuple[str, str]]],
                 refresh_seconds: float = 60.0, clock: Callable[[], float] = time.time) -> None:
        self._engines = engines
        self._http_get = http_get
        self._read_logs = read_logs
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._records: list[Mapping[str, Any]] = []
        self._records_at = float("-inf")
        self._log_readers: dict[str, NinferLogReader] = {}

    def _measured(self) -> list[Mapping[str, Any]]:
        now = self._clock()
        if now - self._records_at >= self._refresh_seconds:
            self._records = [r for r in self._engines().values()
                             if (r.get("metrics") or {}).get("format", "none") != "none"]
            self._records_at = now
        return self._records

    def collect(self, running_services: set[str] | None) -> list[EngineSample]:
        """One sample per measured engine. `running_services` (None: unknown) decides whether a
        log-read engine is up, since its log stays readable after it stops."""
        samples = []
        for record in self._measured():
            labels = {"engine": str(record["service"]), "model": str(record["id"]),
                      "device": str(record.get("device", "")), "kind": str(record.get("kind", ""))}
            fmt = record["metrics"]["format"]
            try:
                if fmt == "llamacpp":
                    samples.append(self._llamacpp(record, labels))
                elif fmt == "ninfer-log":
                    samples.append(self._ninfer(record, labels, running_services))
                else:
                    logger.warning("engine metrics: %s has unknown metrics format %r", labels["engine"], fmt)
            except Exception as e:  # noqa: BLE001 - one unreadable engine is reported down, not a failed scrape
                logger.info("engine metrics: cannot read %s: %s", labels["engine"], e)
                samples.append(EngineSample(labels, up=False))
        return samples

    def _llamacpp(self, record: Mapping[str, Any], labels: dict[str, str]) -> EngineSample:
        metrics = record["metrics"]
        url = f"http://{record['service']}:{metrics['port']}{metrics['path']}"
        counters, gauges = parse_llamacpp(self._http_get(url))
        return EngineSample(labels, up=True, counters=counters, gauges=gauges)

    def _ninfer(self, record: Mapping[str, Any], labels: dict[str, str],
                running_services: set[str] | None) -> EngineSample:
        service = str(record["service"])
        if running_services is not None and service not in running_services:
            return EngineSample(labels, up=False)
        reader = self._log_readers.setdefault(service, NinferLogReader(self._clock()))
        reader.consume(self._read_logs(service, reader.since, NINFER_LOG_TAIL))
        return EngineSample(labels, up=True, counters=dict(reader.counters), gauges=dict(reader.gauges))
