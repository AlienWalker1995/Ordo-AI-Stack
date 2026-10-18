"""GET /services/{id}/logs and the lifecycle refusals: both streams, a cursor, a byte cap, typed codes.

The drawer on the dashboard's Services page was empty for every service that logs to stderr
(llama.cpp, caddy, uvicorn), always showed 100 lines whatever `tail` it asked for, and could not
poll for only the new lines. These pin the parsing and shaping (ordo/control/logs.py), what the
route passes to the backend, what DockerBackend runs, and the 400/403/404 the routes answer.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from ordo.control import logs
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, DockerBackend, MockBackend, NoContainer
from ordo.control.scheduler import Scheduler

COMPOSE = {"services": {"web": {"image": "busybox"}, "agent": {"image": "hermes"},
                        "ops-controller": {"image": "ordo"}}}


@pytest.fixture
def backend():
    return MockBackend(COMPOSE)


@pytest.fixture
def cp(tmp_path, backend):
    broker = Broker(Scheduler(total_vram_gb=24.0), backend)
    return ControlPlane(source_path=tmp_path / "ordo.yaml", catalog=MagicMock(), registry=MagicMock(),
                        out_dir=tmp_path, scheduler=broker.scheduler, broker=broker)


# --------------------------------------------------------------------------- #
# The query: tail and since.
# --------------------------------------------------------------------------- #


def test_the_query_defaults_to_200_lines_from_the_start():
    assert logs.parse_query({}) == logs.LogQuery(tail=200, since=None)
    assert logs.parse_query({"tail": "", "since": ""}) == logs.LogQuery(tail=200, since=None)


@pytest.mark.parametrize("tail", ["1", "300", "1000"])
def test_a_tail_in_range_is_taken(tail):
    assert logs.parse_query({"tail": tail}).tail == int(tail)


@pytest.mark.parametrize("tail", ["0", "-5", "1001", "abc", "1.5"])
def test_a_tail_out_of_range_or_not_an_integer_is_refused(tail):
    with pytest.raises(logs.LogQueryError):
        logs.parse_query({"tail": tail})


@pytest.mark.parametrize("since", ["2026-10-17T04:10:21.044180797Z", "2026-10-17T04:10:21Z",
                                   "2026-10-17T00:10:21.5-04:00", "10m", "1h30m", "1.5h", "45s"])
def test_a_since_docker_reads_as_a_timestamp_or_a_duration_is_taken(since):
    assert logs.parse_query({"since": since}).since == since


@pytest.mark.parametrize("since", ["yesterday", "1700000000", "2026-10-17", "10x", "2026-13-01T00:00:00Z",
                                   "2026-10-17T04:10:21.0441807971Z", "-10m"])
def test_any_other_since_is_refused_rather_than_guessed_at(since):
    with pytest.raises(logs.LogQueryError):
        logs.parse_query({"since": since})


# --------------------------------------------------------------------------- #
# Merging the two streams, and shaping the answer.
# --------------------------------------------------------------------------- #


def test_the_streams_are_interleaved_by_time():
    stdout = ("2026-10-17T04:00:00.000000001Z out one\n"
              "2026-10-17T04:00:00.000000003Z out three\n")
    stderr = ("2026-10-17T04:00:00.000000002Z err two\n"
              "2026-10-17T04:00:00.000000004Z err four\n")
    assert [text for _ts, text in logs.merge_streams(stdout, stderr)] == ["out one", "err two", "out three", "err four"]


def test_a_tie_keeps_stdout_first_and_a_stream_keeps_its_own_order():
    same = "2026-10-17T04:00:00.000000001Z"
    merged = logs.merge_streams(f"{same} out a\n{same} out b\n", f"{same} err c\n")
    assert [text for _ts, text in merged] == ["out a", "out b", "err c"]


def test_a_line_keeps_its_spaces_and_an_empty_line_survives():
    merged = logs.merge_streams("2026-10-17T04:00:00.000000001Z  indented  text\n"
                                "2026-10-17T04:00:00.000000002Z \n", "")
    assert merged == [("2026-10-17T04:00:00.000000001Z", " indented  text"), ("2026-10-17T04:00:00.000000002Z", "")]


def test_the_answer_is_plain_text_with_the_cursor_beside_it():
    lines = [("2026-10-17T04:00:00.000000001Z", "one"), ("2026-10-17T04:00:00.000000002Z", "two")]
    assert logs.payload(lines, logs.LogQuery(tail=200, since=None)) == {
        "logs": "one\ntwo\n", "tail": 200, "since": None,
        "last_ts": "2026-10-17T04:00:00.000000002Z", "truncated": False,
    }


def test_no_line_is_an_empty_text_and_no_cursor():
    """A duration names no point in time, so with nothing new there is no cursor to answer."""
    assert logs.payload([], logs.LogQuery(tail=5, since="10m")) == {
        "logs": "", "tail": 5, "since": "10m", "last_ts": None, "truncated": False}


def test_an_idle_poll_answers_the_incoming_cursor_not_null():
    """A caller stores `last_ts` and sends it back; a null would reset it to the full tail."""
    since = "2026-10-17T04:00:00.000000002Z"
    answer = logs.payload([("2026-10-17T04:00:00.000000002Z", "seen")], logs.LogQuery(tail=200, since=since))
    assert answer["logs"] == "" and answer["last_ts"] == since


def test_an_idle_poll_normalises_the_cursor_to_the_form_docker_stamps():
    answer = logs.payload([], logs.LogQuery(tail=200, since="2026-10-17T00:00:00.5-04:00"))
    assert answer["last_ts"] == "2026-10-17T04:00:00.500000000Z"


def test_an_absolute_since_drops_the_line_docker_returns_at_it():
    """docker's --since is inclusive, so `since=last_ts` would repeat the last line every poll."""
    lines = [("2026-10-17T04:00:00.000000001Z", "seen"), ("2026-10-17T04:00:00.000000002Z", "new")]
    answer = logs.payload(lines, logs.LogQuery(tail=200, since="2026-10-17T04:00:00.000000001Z"))
    assert answer["logs"] == "new\n" and answer["last_ts"] == "2026-10-17T04:00:00.000000002Z"


def test_since_compares_instants_not_strings():
    """An offset and a shorter fraction name the same instant as docker's UTC nanosecond stamp."""
    lines = [("2026-10-17T04:00:00.500000000Z", "at"), ("2026-10-17T04:00:00.500000001Z", "after")]
    answer = logs.payload(lines, logs.LogQuery(tail=200, since="2026-10-17T00:00:00.5-04:00"))
    assert answer["logs"] == "after\n"


def test_the_body_is_capped_in_bytes_keeping_the_newest_lines():
    line = "x" * 1023   # 1 KiB with its newline
    lines = [(f"2026-10-17T04:00:00.{i:09d}Z", f"{i:04d}{line[4:]}") for i in range(300)]
    answer = logs.payload(lines, logs.LogQuery(tail=300, since=None))
    assert answer["truncated"] is True
    assert len(answer["logs"].encode()) == logs.BODY_MAX_BYTES
    kept = answer["logs"].splitlines()
    assert len(kept) == 256 and kept[0].startswith("0044") and kept[-1].startswith("0299")
    assert answer["last_ts"] == "2026-10-17T04:00:00.000000299Z"


def test_one_line_over_the_whole_cap_keeps_its_end():
    huge = "a" * logs.BODY_MAX_BYTES + "END"
    answer = logs.payload([("2026-10-17T04:00:00.000000001Z", huge)], logs.LogQuery(tail=1, since=None))
    assert answer["truncated"] is True and answer["logs"].endswith("END\n")
    assert len(answer["logs"].encode()) == logs.BODY_MAX_BYTES


# --------------------------------------------------------------------------- #
# The route.
# --------------------------------------------------------------------------- #


def test_the_route_passes_tail_and_since_to_the_backend(cp, backend):
    status, body = cp.route("GET", "/services/web/logs", {}, {"tail": "300", "since": "10m"})
    assert status == 200
    assert backend.log_requests == [("web", 300, "10m")]
    assert body["service"] == "web" and body["tail"] == 300 and body["since"] == "10m"


def test_the_answer_keeps_the_logs_text_the_dashboard_reads(cp, backend):
    backend.project_containers["web"].output.extend(["one", "two", "three"])
    status, body = cp.route("GET", "/services/web/logs", {}, {"tail": "2"})
    assert status == 200 and body["logs"] == "two\nthree\n" and body["truncated"] is False


def test_polling_with_last_ts_returns_only_the_new_lines(cp, backend):
    container = backend.project_containers["web"]
    container.output.extend(["one", "two"])
    first = cp.route("GET", "/services/web/logs", {}, {})[1]
    container.output.append("three")
    second = cp.route("GET", "/services/web/logs", {}, {"since": first["last_ts"]})[1]
    assert second["logs"] == "three\n"
    third = cp.route("GET", "/services/web/logs", {}, {"since": second["last_ts"]})[1]
    assert third["logs"] == "" and third["last_ts"] == second["last_ts"]


def test_the_cursor_survives_an_idle_poll_and_then_advances(cp, backend):
    container = backend.project_containers["web"]
    container.output.append("one")
    cursor = cp.route("GET", "/services/web/logs", {}, {})[1]["last_ts"]
    for _ in range(2):  # idle polls: nothing new, the cursor stays put
        idle = cp.route("GET", "/services/web/logs", {}, {"since": cursor})[1]
        assert idle["logs"] == "" and idle["last_ts"] == cursor
        cursor = idle["last_ts"]
    container.output.append("two")
    fresh = cp.route("GET", "/services/web/logs", {}, {"since": cursor})[1]
    assert fresh["logs"] == "two\n"
    assert logs.instant(fresh["last_ts"]) > logs.instant(cursor)


@pytest.mark.parametrize("query", [{"tail": "0"}, {"tail": "x"}, {"since": "yesterday"}])
def test_a_bad_query_is_400_and_never_reaches_the_backend(cp, backend, query):
    status, body = cp.route("GET", "/services/web/logs", {}, query)
    assert status == 400 and body["error"]
    assert backend.log_requests == []


def test_logs_of_a_service_with_no_container_is_404(cp):
    status, body = cp.route("GET", "/services/nope/logs", {}, {})
    assert status == 404 and "nope" in body["error"]


def test_logs_of_a_name_that_is_no_service_name_is_400(cp):
    assert cp.route("GET", "/services/a/b/logs", {}, {})[0] == 400


def test_logs_of_the_control_plane_itself_are_readable(cp):
    """Only cycling those services is refused: reading their logs changes nothing."""
    assert cp.route("GET", "/services/agent/logs", {}, {})[0] == 200


# --------------------------------------------------------------------------- #
# The refusals of the lifecycle verbs.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "recreate"])
@pytest.mark.parametrize("service", ["agent", "ops-controller", "ordo-agent-1"])
def test_cycling_the_control_plane_itself_is_403_and_acts_on_nothing(cp, backend, verb, service):
    status, body = cp.route("POST", f"/services/{service}/{verb}", {"confirm": True})
    assert status == 403
    assert "use docker/compose from the host" in body["error"]
    assert backend.started == backend.stopped == backend.restarted == backend.recreate_calls == []


def test_a_dry_run_of_a_refused_verb_is_refused_too(cp):
    assert cp.route("POST", "/services/agent/restart", {"dry_run": True})[0] == 403


@pytest.mark.parametrize("verb", ["start", "stop", "restart", "recreate"])
def test_a_name_that_is_no_service_name_is_400(cp, verb):
    status, body = cp.route("POST", f"/services/a/b/{verb}", {"confirm": True})
    assert status == 400 and "not a valid compose service name" in body["error"]


# --------------------------------------------------------------------------- #
# What DockerBackend runs.
# --------------------------------------------------------------------------- #


class FakeDockerLogs:
    """`docker` as DockerBackend sees it: `ps` resolves the service to one container, and `logs`
    answers on BOTH of its streams, as the CLI does for a container that writes to both."""

    def __init__(self, returncode: int = 0, containers: str = "ordo-web-1\n"):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.returncode = returncode
        self.containers = containers

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, self.containers, "")
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", "Error: No such container: ordo-web-1")
        stdout = "2026-10-17T04:00:00.000000001Z listening\n2026-10-17T04:00:00.000000003Z ready\n"
        stderr = "2026-10-17T04:00:00.000000002Z WARN slow start\n"
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)


@pytest.fixture
def docker(monkeypatch):
    from ordo.control import broker as broker_module
    fake = FakeDockerLogs()
    monkeypatch.setattr(broker_module.subprocess, "run", fake)
    return DockerBackend(project="ordo"), fake


def test_docker_logs_reads_both_streams_with_timestamps_in_time_order(docker):
    backend, fake = docker
    lines = backend.logs("web", tail=50, since="2026-10-17T03:00:00Z")
    assert [text for _ts, text in lines] == ["listening", "WARN slow start", "ready"]
    argv = fake.calls[-1]
    assert argv == ["docker", "logs", "--timestamps", "--tail", "50", "--since", "2026-10-17T03:00:00Z",
                    "ordo-web-1"]
    assert fake.kwargs[-1]["capture_output"] is True and fake.kwargs[-1]["errors"] == "replace"


def test_docker_logs_without_since_passes_none(docker):
    backend, fake = docker
    backend.logs("web")
    assert "--since" not in fake.calls[-1] and fake.calls[-1][-3:] == ["--tail", "200", "ordo-web-1"]


def test_docker_logs_of_a_service_with_no_container_is_no_container(monkeypatch):
    from ordo.control import broker as broker_module
    monkeypatch.setattr(broker_module.subprocess, "run", FakeDockerLogs(containers=""))
    with pytest.raises(NoContainer):
        DockerBackend(project="ordo").logs("web")


def test_a_failed_docker_logs_is_an_error_not_log_text(monkeypatch):
    from ordo.control import broker as broker_module
    monkeypatch.setattr(broker_module.subprocess, "run", FakeDockerLogs(returncode=1))
    with pytest.raises(RuntimeError, match="No such container"):
        DockerBackend(project="ordo").logs("web")
