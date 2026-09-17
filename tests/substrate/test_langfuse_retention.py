"""services/langfuse/langfuse_retention.py: trace retention through Langfuse's public API.

Two layers of fake: a FakeApi for the run logic (enumeration, dedupe, batching, the per-run cap,
failure handling) and a local HTTP server for HttpLangfuseApi, so the exact requests the module
sends (paths, query parameters, Basic auth, the DELETE body) are pinned against the v4.36 contract
rather than assumed."""
from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "langfuse"))

import langfuse_retention as lr  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
ENV = {"LANGFUSE_PUBLIC_KEY": "pk-lf-test", "LANGFUSE_SECRET_KEY": "sk-lf-test", "LANGFUSE_RETENTION_DAYS": "90"}


class FakeApi:
    def __init__(self, pages: list[list[dict]], fail_delete: bool = False) -> None:
        self.pages = pages
        self.cutoffs: list[str] = []
        self.cursors: list[str | None] = []
        self.deleted: list[list[str]] = []
        self.fail_delete = fail_delete

    def observations_before(self, cutoff_iso, cursor):
        self.cutoffs.append(cutoff_iso)
        self.cursors.append(cursor)
        index = int(cursor) if cursor else 0
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else None
        return self.pages[index], next_cursor

    def delete_traces(self, trace_ids):
        if self.fail_delete:
            raise lr.ApiError("DELETE /api/public/traces returned HTTP 500: boom")
        self.deleted.append(list(trace_ids))


def _obs(*trace_ids: str) -> list[dict]:
    return [{"id": f"o-{i}", "traceId": t} for i, t in enumerate(trace_ids)]


# ── run logic ──────────────────────────────────────────────────────────────────

def test_cutoff_is_retention_days_before_now_in_zulu_form():
    assert lr.cutoff_for(NOW, 90) == "2026-06-17T12:00:00.000Z"


def test_distinct_trace_ids_across_pages_in_first_seen_order():
    api = FakeApi([_obs("a", "b", "a"), _obs("c", "b")])
    ids, capped = lr.expired_trace_ids(api, "cutoff")
    assert ids == ["a", "b", "c"] and capped is False
    assert api.cursors == [None, "1"]


def test_rows_without_a_trace_id_are_ignored():
    ids, _ = lr.expired_trace_ids(FakeApi([[{"id": "x"}, {"traceId": ""}, {"traceId": "t"}]]), "cutoff")
    assert ids == ["t"]


def test_nothing_expired_deletes_nothing():
    api = FakeApi([[]])
    report = lr.run_once(api, 90, NOW, log=lambda _m: None)
    assert (report.traces_found, report.traces_deleted, report.delete_requests) == (0, 0, 0)
    assert api.deleted == []
    assert api.cutoffs == ["2026-06-17T12:00:00.000Z"]


def test_deletes_in_batches_of_the_server_limit():
    ids = [f"t{i}" for i in range(2500)]
    api = FakeApi([_obs(*ids[:1000]), _obs(*ids[1000:])])
    report = lr.run_once(api, 90, NOW, log=lambda _m: None)
    assert [len(b) for b in api.deleted] == [1000, 1000, 500]
    assert sum(api.deleted, []) == ids
    assert (report.traces_found, report.traces_deleted, report.delete_requests) == (2500, 2500, 3)


def test_per_run_cap_bounds_the_work_and_reports_it():
    api = FakeApi([_obs("a", "b", "c", "d")])
    report = lr.run_once(api, 90, NOW, log=lambda _m: None, max_traces=3)
    assert api.deleted == [["a", "b", "c"]]
    assert report.capped is True


def test_api_failure_is_exit_1_and_recorded_unhealthy(tmp_path):
    status = tmp_path / "status.json"
    api = FakeApi([_obs("a")], fail_delete=True)
    assert lr.run_and_record(api, 90, status, now=lambda: NOW) == 1
    assert json.loads(status.read_text())["ok"] is False
    assert lr.healthcheck(status) == 1


def test_success_is_recorded_healthy(tmp_path):
    status = tmp_path / "status.json"
    assert lr.run_and_record(FakeApi([_obs("a")]), 90, status, now=lambda: NOW) == 0
    recorded = json.loads(status.read_text())
    assert recorded["ok"] is True and recorded["traces_deleted"] == 1
    assert lr.healthcheck(status) == 0


def test_healthcheck_is_healthy_before_any_run(tmp_path):
    assert lr.healthcheck(tmp_path / "absent.json") == 0


# ── configuration ──────────────────────────────────────────────────────────────

def test_settings_from_env_defaults():
    s = lr.settings_from_env(ENV)
    assert (s.base_url, s.retention_days, s.run_at) == ("http://langfuse-web:3000", 90, (4, 45))


@pytest.mark.parametrize("days", ["", "0", "-3", "ninety", "1.5"])
def test_invalid_retention_days_is_a_config_error(days):
    with pytest.raises(lr.ConfigError):
        lr.settings_from_env({**ENV, "LANGFUSE_RETENTION_DAYS": days})


def test_missing_keys_is_a_config_error():
    with pytest.raises(lr.ConfigError):
        lr.settings_from_env({"LANGFUSE_RETENTION_DAYS": "90"})


@pytest.mark.parametrize("value", ["4", "24:00", "04:60", "aa:bb", "1:2:3"])
def test_invalid_run_at_is_a_config_error(value):
    with pytest.raises(lr.ConfigError):
        lr.parse_run_at(value)


def test_main_exits_2_on_bad_configuration():
    assert lr.main(["--once"], {"LANGFUSE_RETENTION_DAYS": "90"}) == 2


def test_seconds_until_next_run_today_and_tomorrow():
    assert lr.seconds_until_next_run(datetime(2026, 9, 15, 4, 0, tzinfo=UTC), (4, 45)) == 45 * 60
    assert lr.seconds_until_next_run(datetime(2026, 9, 15, 4, 45, tzinfo=UTC), (4, 45)) == 24 * 3600


def test_a_slot_seconds_away_is_treated_as_served_not_slept_through():
    """time.sleep wakes early, so waking at 04:44:59.5 must not run the job a second time at
    04:45:00 (observed live 2026-09-16: two runs 2.5 seconds apart)."""
    almost = datetime(2026, 9, 15, 4, 44, 59, 500000, tzinfo=UTC)
    assert lr.seconds_until_next_run(almost, (4, 45)) == pytest.approx(24 * 3600 + 0.5)


def test_daemon_waits_for_start_delay_then_the_daily_slot(tmp_path):
    sleeps: list[float] = []
    settings = lr.settings_from_env(ENV)
    lr.daemon(FakeApi([[]]), settings, tmp_path / "status.json", sleep=sleeps.append, max_runs=2)
    assert sleeps[0] == lr.STARTUP_DELAY_SECONDS
    assert len(sleeps) == 3 and all(0 < s <= 24 * 3600 for s in sleeps[1:])
    assert json.loads((tmp_path / "status.json").read_text())["ok"] is True


# ── the real HTTP client against a local fake Langfuse ─────────────────────────

class _FakeLangfuse(BaseHTTPRequestHandler):
    requests: list[dict] = []
    fail_with: int | None = None

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _record(self, body: bytes | None = None) -> None:
        type(self).requests.append({"method": self.command, "path": self.path,
                                    "auth": self.headers.get("Authorization"),
                                    "body": json.loads(body) if body else None})

    def _reply(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._record()
        if type(self).fail_with:
            self._reply(type(self).fail_with, {"message": "nope"})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "cursor" not in query:
            self._reply(200, {"data": [{"id": "o1", "traceId": "t1"}, {"id": "o2", "traceId": "t2"}],
                              "meta": {"cursor": "page2"}})
        else:
            self._reply(200, {"data": [{"id": "o3", "traceId": "t1"}], "meta": {}})

    def do_DELETE(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(length))
        self._reply(200, {"message": "Traces deleted successfully"})


@pytest.fixture
def fake_langfuse():
    _FakeLangfuse.requests = []
    _FakeLangfuse.fail_with = None
    server = HTTPServer(("127.0.0.1", 0), _FakeLangfuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _FakeLangfuse
    server.shutdown()


def test_http_client_speaks_the_v4_public_api(fake_langfuse):
    base_url, handler = fake_langfuse
    api = lr.HttpLangfuseApi(base_url, "pk-lf-test", "sk-lf-test", timeout=5)
    report = lr.run_once(api, 90, NOW, log=lambda _m: None)
    assert (report.traces_found, report.traces_deleted) == (2, 2)

    expected_auth = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    gets = [r for r in handler.requests if r["method"] == "GET"]
    assert len(gets) == 2
    first = urllib.parse.urlsplit(gets[0]["path"])
    assert first.path == "/api/public/v2/observations"
    assert urllib.parse.parse_qs(first.query) == {
        "toStartTime": ["2026-06-17T12:00:00.000Z"], "fields": ["core"], "limit": ["1000"]}
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(gets[1]["path"]).query)["cursor"] == ["page2"]

    deletes = [r for r in handler.requests if r["method"] == "DELETE"]
    assert deletes == [{"method": "DELETE", "path": "/api/public/traces", "auth": expected_auth,
                        "body": {"traceIds": ["t1", "t2"]}}]
    assert all(r["auth"] == expected_auth for r in handler.requests)


def test_http_client_fails_fast_on_4xx(fake_langfuse):
    base_url, handler = fake_langfuse
    handler.fail_with = 401
    sleeps: list[float] = []
    api = lr.HttpLangfuseApi(base_url, "pk", "sk", timeout=5, sleep=sleeps.append)
    with pytest.raises(lr.ApiError, match="HTTP 401"):
        api.observations_before("cutoff", None)
    assert sleeps == [] and len(handler.requests) == 1


def test_http_client_retries_5xx_then_fails(fake_langfuse):
    base_url, handler = fake_langfuse
    handler.fail_with = 503
    sleeps: list[float] = []
    api = lr.HttpLangfuseApi(base_url, "pk", "sk", timeout=5, sleep=sleeps.append)
    with pytest.raises(lr.ApiError, match="HTTP 503"):
        api.observations_before("cutoff", None)
    assert sleeps == list(lr.RETRY_BACKOFF_SECONDS)
    assert len(handler.requests) == len(lr.RETRY_BACKOFF_SECONDS) + 1
