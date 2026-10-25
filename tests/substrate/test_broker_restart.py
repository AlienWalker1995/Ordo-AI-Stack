"""DockerBackend's stop/restart seam: the exit-event race cannot leave a container stopped.

`docker restart` is not atomic: on a daemon race ("tried to kill container, but did not receive
an exit event") the stop half lands and the start half never runs, and the manual-stop mark keeps
unless-stopped from reviving the container (observed live 2026-10-25: the idle-RAM reclaim left
ComfyUI Exited(137) until a human noticed; the 2026-10-19 deploy failed its `docker stop` on the
same race). Every stop/restart therefore goes through the broker's helpers, which verify the end
state with `docker inspect` instead of trusting the CLI's exit code.

The daemon is faked: `subprocess.run` is patched, so nothing touches docker.
"""
from __future__ import annotations

import subprocess

import pytest

from ordo.control import broker as broker_module
from ordo.control.broker import ContainerLifecycleError, DockerBackend


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


RACE_STDERR = ("Error response from daemon: Cannot restart container c1: tried to kill container, "
               "but did not receive an exit event")


class FakeDaemon:
    """A scriptable docker daemon: per-container running state plus scripted stop/start outcomes
    and inspect answers."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.running: dict[str, bool] = {}
        self.stop_results: list[Result] = []   # popped per `docker stop`; default success
        self.start_results: list[Result] = []  # popped per `docker start`; default success
        self.inspect_answers: list[str] = []   # scripted State.Running; empty falls back to `running`
        self.ps_names: list[str] = []          # what `docker ps ... --format {{.Names}}` returns

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        verb = cmd[1]
        if verb == "ps":
            return Result(0, "".join(name + "\n" for name in self.ps_names))
        if verb == "stop":
            result = self.stop_results.pop(0) if self.stop_results else Result()
            if result.returncode == 0:
                self.running[cmd[2]] = False
            return result
        if verb == "start":
            result = self.start_results.pop(0) if self.start_results else Result()
            if result.returncode == 0:
                self.running[cmd[2]] = True
            return result
        if verb == "inspect":
            answer = (self.inspect_answers.pop(0) if self.inspect_answers
                      else ("true" if self.running.get(cmd[-1]) else "false"))
            if answer == "gone":
                return Result(1, "", f"Error: No such object: {cmd[-1]}")
            return Result(0, answer + "\n")
        raise AssertionError(f"unexpected docker argv: {cmd}")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def seam(monkeypatch):
    """A DockerBackend whose docker CLI is the fake daemon, on a fake clock."""
    daemon, clock = FakeDaemon(), FakeClock()
    monkeypatch.setattr(broker_module.subprocess, "run", daemon.run)
    monkeypatch.setattr(broker_module.time, "sleep", clock.sleep)
    monkeypatch.setattr(broker_module.time, "monotonic", clock.monotonic)
    return DockerBackend("ordo"), daemon, clock


def _verbs(daemon):
    return [c[1] for c in daemon.calls]


def test_a_clean_restart_stops_then_starts_and_confirms_running(seam):
    backend, daemon, _clock = seam
    daemon.running["c1"] = True
    backend._docker_restart("c1")
    assert _verbs(daemon) == ["stop", "start", "inspect"]
    assert daemon.calls[0][2:] == ["c1"]
    assert daemon.calls[1][2:] == ["c1"]
    assert daemon.calls[2][2:] == ["--format", "{{.State.Running}}", "c1"]
    assert daemon.running["c1"] is True


def test_a_raced_stop_waits_for_the_exit_to_land_before_starting(seam):
    backend, daemon, clock = seam
    daemon.running["c1"] = True
    daemon.stop_results.append(Result(1, "", RACE_STDERR))
    daemon.inspect_answers.extend(["true", "true", "false"])  # the exit lands two polls later
    backend._docker_restart("c1")
    verbs = _verbs(daemon)
    assert verbs.count("stop") == 1 and verbs.count("start") == 1
    assert verbs.index("stop") < verbs.index("start") < verbs.index("inspect", verbs.index("start"))
    assert verbs.count("inspect") == 4  # three exit-wait polls + the start confirmation
    assert clock.now == 4.0  # it waited out the race at the poll interval, then started
    assert daemon.running["c1"] is True


def test_a_raced_stop_that_never_exits_raises_and_never_starts(seam):
    backend, daemon, clock = seam
    daemon.running["c1"] = True
    daemon.stop_results.append(Result(1, "", RACE_STDERR))
    daemon.inspect_answers.extend(["true"] * 1000)  # still running at every poll
    with pytest.raises(ContainerLifecycleError, match="still running"):
        backend._docker_restart("c1")
    assert "start" not in _verbs(daemon)  # the start half never ran
    assert clock.now >= 120.0  # the bound elapsed before giving up


def test_a_failed_start_raises_after_the_stop_half_landed(seam):
    backend, daemon, _clock = seam
    daemon.running["c1"] = True
    daemon.start_results.append(Result(1, "",
                                       "Error response from daemon: driver failed: device busy"))
    with pytest.raises(ContainerLifecycleError, match="docker start"):
        backend._docker_restart("c1")
    assert daemon.running["c1"] is False  # the stop half landed
    assert "inspect" not in _verbs(daemon)  # no confirmation after a failed start


def test_a_start_that_succeeds_but_leaves_the_container_down_raises(seam):
    backend, daemon, clock = seam
    daemon.start_results.append(Result())  # docker start exits 0...
    daemon.inspect_answers.extend(["false"] * 1000)  # ...but the container is not running
    with pytest.raises(ContainerLifecycleError, match="not running"):
        backend._docker_start("c1")
    assert clock.now >= 10.0  # the confirmation bound elapsed


def test_a_clean_stop_needs_no_confirmation(seam):
    backend, daemon, _clock = seam
    daemon.running["c1"] = True
    backend._docker_stop("c1")
    assert _verbs(daemon) == ["stop"]
    assert daemon.running["c1"] is False


def test_a_non_race_stop_failure_keeps_called_process_error_semantics(seam):
    backend, daemon, _clock = seam
    daemon.stop_results.append(Result(125, "", "Error response from daemon: container c1 is paused"))
    with pytest.raises(subprocess.CalledProcessError):
        backend._docker_stop("c1")
    assert "inspect" not in _verbs(daemon)


def test_a_gone_container_counts_as_exited_when_waiting_out_a_race(seam):
    backend, daemon, _clock = seam
    daemon.running["c1"] = True
    daemon.stop_results.append(Result(1, "", RACE_STDERR))
    daemon.inspect_answers.append("gone")  # the container was removed while we waited
    backend._docker_stop("c1")
    assert _verbs(daemon) == ["stop", "inspect"]


def test_the_service_restart_verb_routes_the_resolved_container_through_the_seam(seam):
    backend, daemon, _clock = seam
    daemon.ps_names = ["ordo-comfyui-1"]
    daemon.running["ordo-comfyui-1"] = True
    backend.restart("comfyui")
    assert _verbs(daemon) == ["ps", "stop", "start", "inspect"]
    assert daemon.calls[1][2:] == ["ordo-comfyui-1"]
