"""One behavioural contract for ContainerBackend, run against MockBackend AND DockerBackend.

Every control-plane test drives MockBackend, so the suite is only as true as the fake. Twice the
fake agreed with its callers while the real backend could not do the same thing, and both times a
green suite shipped production 500s:

  - 0867df4: DockerBackend lacked ten methods the routes called (`'DockerBackend' object has no
    attribute 'list_services'`). test_backend_protocol_completeness.py now checks the names.
  - 62492fd: the owner of a `network_mode: service:<owner>` namespace was recreated, restarted or
    taken down alone, leaving its members running, "healthy" and holding only `lo`.

Names are not behaviour. Each test below is written once and runs twice: against the fake, and
against the real backend on a throwaway compose project of pinned busybox containers (never the
stack's own project). A behaviour the fake gets wrong, or the real backend lacks, fails here.

The docker half is marked `docker`: it skips where no docker daemon answers, and the CI job that
exists to run it sets ORDO_REQUIRE_DOCKER=1, which turns that skip into a failure (tests/conftest.py).

What the protocol cannot observe (what a container printed, whether a member still has a network)
goes through a small harness per backend: raw docker for the real one, the model for the fake.
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

from ordo.control.broker import DockerBackend, MockBackend
from ordo.render.changed_set import diff_services

# Multi-arch index digest of busybox 1.37.0: tiny, has `sh`, `sleep`, `ls` and `true`.
BUSYBOX = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
# `init: true` so PID 1 is docker-init, which forwards SIGTERM: `docker stop` is instant.
SLEEPER = {"image": BUSYBOX, "command": ["sleep", "86400"], "init": True}

COMPOSE = {
    "services": {
        "db": {**SLEEPER, "healthcheck": {"test": ["CMD", "true"], "interval": "1s", "timeout": "5s",
                                          "retries": 3}},
        "web": {**SLEEPER, "depends_on": ["db"]},
        # A netns owner and its member, in the shape the renderer emits (ordo/render/compose.py):
        # `depends_on.<owner>.restart: true` is what makes compose restart the member with it.
        "owner": dict(SLEEPER),
        "member": {**SLEEPER, "network_mode": "service:owner",
                   "depends_on": {"owner": {"condition": "service_started", "restart": True}}},
        # A one-shot job (`restart: "no"`), like evals: runs to completion and stays exited.
        "job": {"image": BUSYBOX, "command": ["true"], "restart": "no"},
        "extra": {**SLEEPER, "profiles": ["extra"]},
    }
}
SERVICES = sorted(COMPOSE["services"])
LONG_RUNNING = [s for s in SERVICES if s != "job"]
ENV_FILES = (".env", "secrets.env", "secret-files.env")   # stack.compose_argv passes all three


# --------------------------------------------------------------------------- #
# The two harnesses.
# --------------------------------------------------------------------------- #


class FakeHarness:
    kind = "fake"

    def __init__(self, project: str):
        self.project = project
        self.backend = MockBackend(copy.deepcopy(COMPOSE), project=project)

    def emit(self, service: str, lines: list[str]) -> None:
        self.backend.project_containers[service].output.extend(lines)

    def attached(self, service: str) -> bool:
        return self.backend.network_attached(service)


class DockerHarness:
    kind = "docker"

    def __init__(self, project: str, compose_dir: Path):
        self.project = project
        self.compose_dir = compose_dir
        compose_dir.mkdir(parents=True, exist_ok=True)
        (compose_dir / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE, sort_keys=False), encoding="utf-8")
        for name in ENV_FILES:
            (compose_dir / name).write_text("", encoding="utf-8")
        self.backend = DockerBackend(project=project)
        self.backend.COMPOSE_DIR = compose_dir.as_posix()

    def compose(self, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-p", self.project, "-f", (self.compose_dir / "docker-compose.yml").as_posix(),
             "--profile", "extra", *args],
            capture_output=True, text=True, timeout=timeout,
        )

    def up(self) -> None:
        proc = self.compose("up", "-d")
        assert proc.returncode == 0, proc.stderr

    def down(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "1")

    def container(self, service: str) -> str:
        return f"{self.project}-{service}-1"

    def emit(self, service: str, lines: list[str]) -> None:
        # Written to PID 1's stdout, which is what `docker logs` reads.
        script = 'for line in "$@"; do echo "$line"; done > /proc/1/fd/1'
        subprocess.run(["docker", "exec", self.container(service), "sh", "-c", script, "sh", *lines],
                       check=True, capture_output=True, timeout=30)

    def attached(self, service: str) -> bool:
        proc = subprocess.run(["docker", "exec", self.container(service), "ls", "/sys/class/net"],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0 and "eth0" in proc.stdout.split()


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=30)
        compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return info.returncode == 0 and compose.returncode == 0


def _make_harness(kind: str, compose_dir: Path):
    """A harness on a fresh project, and the teardown for it."""
    project = f"ordo-contract-{uuid.uuid4().hex[:8]}"
    if kind == "fake":
        return FakeHarness(project), lambda: None
    if not _docker_available():
        pytest.skip("docker is not reachable")
    real = DockerHarness(project, compose_dir)
    try:
        real.up()
    except BaseException:
        real.down()
        raise
    return real, real.down


BACKENDS = ["fake", pytest.param("docker", marks=pytest.mark.docker)]


@pytest.fixture(params=BACKENDS)
def harness(request, tmp_path):
    """A fresh project per test: for every test that changes it."""
    made, teardown = _make_harness(request.param, tmp_path / "stack")
    try:
        yield made
    finally:
        teardown()


@pytest.fixture(scope="module", params=BACKENDS)
def unchanged(request, tmp_path_factory):
    """One project for the whole module: only for tests that must leave it exactly as it was
    (reads, and calls that are refused or change nothing). A real stack per test costs seconds."""
    made, teardown = _make_harness(request.param, tmp_path_factory.mktemp("stack"))
    try:
        yield made
    finally:
        teardown()


# --------------------------------------------------------------------------- #
# Helpers over the protocol's own reads.
# --------------------------------------------------------------------------- #


def _rows(backend) -> dict[str, dict]:
    return {row["id"]: row for row in backend.list_services()["services"]}


def _state(backend, service: str) -> str | None:
    row = _rows(backend).get(service)
    return row["state"] if row else None


def _container_id(backend, service: str) -> str:
    return backend.stack_state().running[service].container_id


def _wait_healthy(backend, service: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        row = _rows(backend)[service]
        if row["health"] == "healthy" or time.monotonic() > deadline:
            return row
        time.sleep(0.5)


# --------------------------------------------------------------------------- #
# Reads.
# --------------------------------------------------------------------------- #


def test_list_services_reports_every_container_of_the_project(unchanged):
    rows = _rows(unchanged.backend)
    assert sorted(rows) == SERVICES
    for service, row in rows.items():
        assert set(row) == {"id", "name", "state", "health", "status"}
        assert row["name"] == f"{unchanged.project}-{service}-1"
    assert all(rows[s]["state"] == "running" and rows[s]["status"].startswith("Up") for s in LONG_RUNNING)
    assert rows["job"]["state"] == "exited" and rows["job"]["status"].startswith("Exited (0)")
    assert rows["web"]["health"] is None, "no healthcheck is not the same as unhealthy"


def test_list_services_carries_the_healthcheck_verdict(unchanged):
    row = _wait_healthy(unchanged.backend, "db")
    assert row["health"] == "healthy" and "(healthy)" in row["status"]


def test_list_containers_is_a_bare_list_that_includes_every_project_container(unchanged):
    _wait_healthy(unchanged.backend, "db")
    listing = unchanged.backend.list_containers()
    assert isinstance(listing, list)
    ours = {row["service"]: row for row in listing if row["project"] == unchanged.project}
    assert sorted(ours) == SERVICES
    for service, row in ours.items():
        assert set(row) == {"name", "status", "image", "project", "service", "health"}
        assert row["name"] == f"{unchanged.project}-{service}-1"
        assert BUSYBOX.startswith(row["image"]) and row["image"]
    assert (ours["web"]["status"], ours["web"]["health"]) == ("running", None)
    assert (ours["db"]["status"], ours["db"]["health"]) == ("running", "healthy")
    assert ours["job"]["status"] == "exited"


def test_container_inspect_summarizes_a_project_container(unchanged):
    _wait_healthy(unchanged.backend, "db")
    fields = {"name", "project", "service", "image", "image_id", "state", "health", "started_at",
              "restart_count", "restart_policy", "mounts", "networks", "ports"}
    for service, state, health in [("db", "running", "healthy"), ("web", "running", None), ("job", "exited", None)]:
        summary = unchanged.backend.container_inspect(f"{unchanged.project}-{service}-1")
        assert set(summary) == fields
        assert (summary["name"], summary["project"], summary["service"]) == (
            f"{unchanged.project}-{service}-1", unchanged.project, service)
        assert (summary["state"], summary["health"]) == (state, health)
        assert summary["image"] == BUSYBOX
    assert unchanged.backend.container_inspect(f"{unchanged.project}-member-1")["networks"] == []


def test_service_stats_covers_every_project_service(unchanged):
    stats = unchanged.backend.service_stats()
    assert set(stats) == {"gpu", "services", "vram_aggregate_unavailable"}
    assert stats["gpu"] is None and stats["vram_aggregate_unavailable"] is True
    assert sorted(stats["services"]) == SERVICES
    for service, row in stats["services"].items():
        assert set(row) == {"cpu_pct", "mem_gb", "mem_pct", "vram_gb", "vram_pct", "running"}
        assert all(isinstance(row[k], float) and row[k] >= 0 for k in row if k != "running")
        assert row["running"] is (service != "job")


def test_rendered_compose_is_the_file_the_verbs_run(unchanged):
    assert unchanged.backend.rendered_compose() == COMPOSE


def test_stack_state_of_a_fresh_project_has_nothing_to_recreate(unchanged):
    state = unchanged.backend.stack_state()
    assert sorted(state.rendered) == SERVICES and sorted(state.running) == SERVICES
    assert state.rendered["job"].one_shot and not state.rendered["web"].one_shot
    assert state.running["web"].state == "running" and state.running["job"].state == "exited"
    assert all(c.compose_version == state.compose_version and c.container_id for c in state.running.values())
    # The changed set `ordo apply` and ops-controller recreate: empty right after an up, members
    # included (their hash names the owner's container, not `service:owner`).
    assert diff_services(state.rendered, state.running, compose_version=state.compose_version) == []


def test_logs_returns_the_last_lines_of_the_service(harness):
    harness.emit("web", ["one", "two", "three"])
    assert harness.backend.logs("web", tail=2) == "two\nthree\n"
    assert harness.backend.logs("web", tail=100) == "one\ntwo\nthree\n"


def test_logs_of_a_service_with_no_container_is_a_message_not_an_error(unchanged):
    assert unchanged.backend.logs("nope") == "[no container found for service nope]"


def test_logs_survive_a_restart(harness):
    harness.emit("web", ["before"])
    harness.backend.restart("web")
    assert harness.backend.logs("web", tail=5) == "before\n"


def test_container_logs_by_container_name(harness):
    harness.emit("web", ["a", "b"])
    assert harness.backend.container_logs(f"{harness.project}-web-1", tail=1) == "b\n"


# --------------------------------------------------------------------------- #
# Per-container verbs.
# --------------------------------------------------------------------------- #


def test_stop_start_and_restart_a_service(harness):
    backend = harness.backend
    backend.stop("web")
    row = _rows(backend)["web"]
    assert row["state"] == "exited" and row["status"].startswith("Exited")
    backend.start("web")
    assert _state(backend, "web") == "running"
    backend.restart("web")
    assert _state(backend, "web") == "running"


def test_start_of_a_running_service_changes_nothing(unchanged):
    before = _container_id(unchanged.backend, "web")
    unchanged.backend.start("web")
    assert _state(unchanged.backend, "web") == "running" and _container_id(unchanged.backend, "web") == before


def test_a_project_qualified_name_acts_on_the_service(harness):
    harness.backend.stop(f"{harness.project}-web-1")
    assert _state(harness.backend, "web") == "exited"


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_a_service_with_no_container_is_a_no_op(unchanged, verb):
    """An abstract lease job (a media lease) has no container: the broker starts and stops it
    anyway, and that must neither fail nor touch anything else."""
    before = _rows(unchanged.backend)
    getattr(unchanged.backend, verb)("lease-job")
    assert {k: v["state"] for k, v in _rows(unchanged.backend).items()} == {k: v["state"] for k, v in before.items()}


@pytest.mark.parametrize("service", ["agent", "ops-controller"])
@pytest.mark.parametrize("verb", ["start", "stop", "restart", "recreate_service", "recreate_services",
                                  "remove_stopped_containers"])
def test_the_control_plane_is_never_cycled_through_itself(unchanged, verb, service):
    argument = [service] if verb in ("recreate_services", "remove_stopped_containers") else service
    with pytest.raises(ValueError):
        getattr(unchanged.backend, verb)(argument)


@pytest.mark.parametrize("name", ["", " web", "web ", "other/web"])
@pytest.mark.parametrize("verb", ["start", "stop", "restart", "logs", "recreate_service",
                                  "compose_up", "compose_down", "compose_restart"])
def test_a_malformed_service_name_is_refused(unchanged, verb, name):
    with pytest.raises(ValueError):
        getattr(unchanged.backend, verb)(name)


def test_container_restart_by_name(harness):
    harness.backend.stop("web")
    harness.backend.container_restart(f"{harness.project}-web-1")
    assert _state(harness.backend, "web") == "running"


@pytest.mark.parametrize("verb", ["container_logs", "container_restart", "container_inspect"])
@pytest.mark.parametrize("name", ["web", "ordo-web-1", "not-a-container", "a/b"])
def test_a_container_outside_the_project_is_refused(unchanged, verb, name):
    with pytest.raises(ValueError):
        getattr(unchanged.backend, verb)(name)


# --------------------------------------------------------------------------- #
# exec.
# --------------------------------------------------------------------------- #


def test_exec_in_returns_the_exit_code_and_the_combined_output(unchanged):
    command = ["sh", "-c", "printf out; printf err >&2; exit 3"]
    if unchanged.kind == "fake":
        unchanged.backend.exec_result = (3, "outerr")   # the fake runs nothing: it is told the result
    assert unchanged.backend.exec_in(f"{unchanged.project}-web-1", command) == (3, "outerr")
    assert unchanged.backend.exec_in_service("web", command) == (3, "outerr")


def test_exec_in_a_stopped_container_fails_without_raising(harness):
    harness.backend.stop("web")
    code, output = harness.backend.exec_in(f"{harness.project}-web-1", ["true"])
    assert code != 0 and "is not running" in output


@pytest.mark.parametrize("name", ["not-a-container", "ordo-web-1", "a/b"])
def test_exec_in_a_container_outside_the_project_is_not_found(unchanged, name):
    with pytest.raises(FileNotFoundError):
        unchanged.backend.exec_in(name, ["true"])


def test_exec_in_a_service_with_no_container_is_not_found(unchanged):
    with pytest.raises(FileNotFoundError):
        unchanged.backend.exec_in_service("nope", ["true"])


# --------------------------------------------------------------------------- #
# Recreate and remove.
# --------------------------------------------------------------------------- #


def test_recreate_makes_a_new_running_container(harness):
    backend = harness.backend
    harness.emit("web", ["old container"])
    before = _container_id(backend, "web")
    backend.recreate_service("web")
    assert _state(backend, "web") == "running"
    assert _container_id(backend, "web") != before
    assert backend.logs("web") == "", "a recreate is a new container, not a restart"


def test_recreate_starts_a_stopped_service_but_not_its_dependencies(harness):
    backend = harness.backend
    backend.stop("db")
    backend.stop("web")
    backend.recreate_service("web")
    assert _state(backend, "web") == "running"
    assert _state(backend, "db") == "exited", "a recreate must be --no-deps"


def test_recreating_a_netns_owner_keeps_its_member_on_the_network(harness):
    """62492fd: recreating the owner alone left the member in the destroyed namespace."""
    member_before = _container_id(harness.backend, "member")
    harness.backend.recreate_service("owner")
    assert _state(harness.backend, "member") == "running"
    assert _container_id(harness.backend, "member") != member_before
    assert harness.attached("member")


def test_recreate_services_recreates_the_whole_batch_in_one_call(harness):
    backend = harness.backend
    before = {s: _container_id(backend, s) for s in ("web", "owner", "member", "db")}
    backend.recreate_services(["web", "owner"])
    after = {s: _container_id(backend, s) for s in before}
    assert [s for s in before if before[s] != after[s]] == ["web", "owner", "member"]
    assert harness.attached("member")


@pytest.mark.parametrize("verb", ["recreate_service", "recreate_services", "remove_stopped_containers"])
def test_a_service_the_file_does_not_define_fails_the_compose_call(unchanged, verb):
    argument = "nope" if verb == "recreate_service" else ["nope"]
    with pytest.raises(subprocess.CalledProcessError):
        getattr(unchanged.backend, verb)(argument)


def test_remove_stopped_containers_removes_only_what_is_stopped(harness):
    backend = harness.backend
    backend.remove_stopped_containers(["job", "web"])
    rows = _rows(backend)
    assert "job" not in rows, "the stopped job container is removed"
    assert rows["web"]["state"] == "running", "a running container is never removed"


# --------------------------------------------------------------------------- #
# Network namespaces: what docker does to a member, and what the group verbs repair.
# --------------------------------------------------------------------------- #


def test_restarting_an_owner_alone_orphans_its_member_and_restarting_the_member_repairs_it(harness):
    """The docker behaviour the control plane's group verbs exist for (ControlPlane restarts the
    members after the owner): a bare restart gives the owner a new namespace and leaves the
    member running in the old one."""
    assert harness.attached("member")
    harness.backend.restart("owner")
    assert _state(harness.backend, "member") == "running"
    assert not harness.attached("member")
    harness.backend.restart("member")
    assert harness.attached("member")


def test_compose_restart_of_an_owner_restarts_its_member_with_it(harness):
    harness.backend.compose_restart("owner")
    assert harness.attached("member")


def test_compose_down_of_an_owner_takes_its_member_down_too(harness):
    """62492fd: a named down left the member running in the removed namespace."""
    harness.backend.compose_down("owner")
    rows = _rows(harness.backend)
    assert "owner" not in rows and "member" not in rows
    assert rows["web"]["state"] == "running", "a named down touches only its group"


def test_compose_up_of_an_owner_brings_its_member_back_on_the_network(harness):
    harness.backend.compose_down("owner")
    harness.backend.compose_up("owner")
    assert _state(harness.backend, "owner") == "running" and _state(harness.backend, "member") == "running"
    assert harness.attached("member")


# --------------------------------------------------------------------------- #
# Project-wide and named compose verbs.
# --------------------------------------------------------------------------- #


def test_compose_up_of_a_service_does_not_start_its_dependencies(harness):
    backend = harness.backend
    backend.stop("db")
    backend.compose_down("web")
    backend.compose_up("web")
    assert _state(backend, "web") == "running"
    assert _state(backend, "db") == "exited", "a named up must be --no-deps"


def test_compose_up_of_a_profiled_service_by_name(harness):
    harness.backend.compose_down("extra")
    harness.backend.compose_up("extra")
    assert _state(harness.backend, "extra") == "running"


def test_whole_project_down_then_up_acts_only_on_unprofiled_services(harness):
    """No profile is active in a bare compose call, so a profiled service (most of the rendered
    stack: comfyui, the edge, hermes-ui) is neither taken down nor brought up by it."""
    backend = harness.backend
    extra_before = _container_id(backend, "extra")
    backend.compose_down()
    assert sorted(_rows(backend)) == ["extra"]
    assert _container_id(backend, "extra") == extra_before
    backend.compose_up()
    rows = _rows(backend)
    assert sorted(rows) == SERVICES
    assert all(rows[s]["state"] == "running" for s in LONG_RUNNING)
    assert harness.attached("member")


def test_whole_project_restart_acts_only_on_unprofiled_services(harness):
    backend = harness.backend
    backend.stop("web")
    backend.stop("extra")
    backend.compose_restart()
    rows = _rows(backend)
    assert all(rows[s]["state"] == "running" for s in LONG_RUNNING if s != "extra")
    assert rows["extra"]["state"] == "exited", "a bare restart does not see a profiled service"
    assert harness.attached("member")


@pytest.mark.parametrize("verb", ["compose_up", "compose_down", "compose_restart"])
def test_a_compose_verb_on_a_service_the_file_does_not_define_fails(unchanged, verb):
    with pytest.raises(subprocess.CalledProcessError):
        getattr(unchanged.backend, verb)("nope")
