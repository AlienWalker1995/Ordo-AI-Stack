"""`ordo deploy`: follow origin/main, but never recreate the stack under a lease, an eval or a turn.

These tests pin the pass: nothing happens when main has not moved, every gate makes the pass wait
(and report once after an hour), a failed deploy is reported once and not retried, the gates are
read again after the build, and a deploy records what it moved from and to. git, docker and the
`ordo` subcommands are fakes behind the Host seam.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ordo.host import bringup, deploy

OLD = "a" * 40
NEW = "b" * 40


class FakeHost:
    def __init__(self, *, target: str = NEW, head: str = OLD, branch: str = "main"):
        self.target = target
        self.head_sha = head
        self.branch_name = branch
        self.dirty: list[str] = []
        self.ancestor = True
        self.leased = False
        self.jobs: list[str] = []
        self.busy: str | None = None
        self.busy_after_build: str | None = None
        self.unknown: str | None = None
        self.build_code = 0
        self.apply_code = 0
        self.calls: list[str] = []
        self.notes: list[str] = []

    def fetch(self) -> str:
        return self.target

    def head(self) -> str:
        return self.head_sha

    def branch(self) -> str:
        return self.branch_name

    def dirty_files(self) -> list[str]:
        return self.dirty

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.ancestor

    def fast_forward(self, sha: str) -> bool:
        self.calls.append("ff")
        self.head_sha = sha
        return True

    def gpu_leased(self) -> bool:
        if self.unknown:
            raise deploy.GateUnknown(self.unknown)
        return self.leased

    def running_jobs(self) -> list[str]:
        return self.jobs

    def agent_busy(self) -> str | None:
        return self.busy

    def build(self, log: Path) -> int:
        self.calls.append("build")
        if self.busy_after_build:
            self.busy = self.busy_after_build
        return self.build_code

    def apply(self, log: Path) -> int:
        self.calls.append("apply")
        return self.apply_code

    def notify(self, text: str) -> None:
        self.notes.append(text)


def state(out: Path) -> dict:
    return json.loads((out / "deploy" / "state.json").read_text(encoding="utf-8"))


def test_a_moved_main_is_fast_forwarded_built_and_applied(tmp_path):
    host = FakeHost()
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert host.calls == ["ff", "build", "apply"]
    saved = state(tmp_path)
    assert saved["deployed"] == NEW and saved["failed"] == "" and saved["waiting"] == ""
    assert saved["last_result"] == f"{OLD[:12]} -> {NEW[:12]}"
    assert host.notes == [f"Deployed `{NEW[:12]}`: {OLD[:12]} -> {NEW[:12]}."]


def test_nothing_happens_when_main_is_the_deployed_commit(tmp_path):
    host = FakeHost()
    deploy.run_once(host, out=tmp_path, now=1000.0)
    host.calls.clear()
    host.notes.clear()
    assert deploy.run_once(host, out=tmp_path, now=2000.0) == 0
    assert host.calls == [] and host.notes == []


@pytest.mark.parametrize("setup, reason", [
    (lambda h: setattr(h, "branch_name", "feat/x"), "the checkout is on feat/x, not main"),
    (lambda h: setattr(h, "dirty", ["ordo/cli.py"]), "the checkout has uncommitted changes (ordo/cli.py)"),
    (lambda h: setattr(h, "ancestor", False), "the checkout's main has commits that are not on origin/main"),
    (lambda h: setattr(h, "leased", True), "a GPU lease is held"),
    (lambda h: setattr(h, "jobs", ["ordo-evals-run-1"]), "a one-off job is running (ordo-evals-run-1)"),
    (lambda h: setattr(h, "busy", "1 turn(s) in flight"), "the agent is busy (1 turn(s) in flight)"),
    (lambda h: setattr(h, "unknown", "docker is down"), "a gate could not be read: docker is down"),
])
def test_every_gate_makes_the_pass_wait(tmp_path, capsys, setup, reason):
    host = FakeHost()
    setup(host)
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert host.calls == []
    assert state(tmp_path)["waiting"] == NEW
    assert reason in capsys.readouterr().out


def test_a_long_wait_is_reported_once(tmp_path):
    host = FakeHost()
    host.leased = True
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=1000.0 + deploy.WAIT_REPORT_SECONDS - 1)
    assert host.notes == []
    deploy.run_once(host, out=tmp_path, now=1000.0 + deploy.WAIT_REPORT_SECONDS)
    deploy.run_once(host, out=tmp_path, now=1000.0 + 2 * deploy.WAIT_REPORT_SECONDS)
    assert host.notes == [f"Deploy of `{NEW[:12]}` has waited 60 min: a GPU lease is held."]


def test_a_wait_is_logged_when_its_reason_changes_not_every_pass(tmp_path, capsys):
    host = FakeHost()
    host.leased = True
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=1120.0)
    host.leased, host.jobs = False, ["ordo-evals-run-1"]
    deploy.run_once(host, out=tmp_path, now=1240.0)
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"Deploy of `{NEW[:12]}` waiting: a GPU lease is held",
                     f"Deploy of `{NEW[:12]}` waiting: a one-off job is running (ordo-evals-run-1)"]


def test_an_unreachable_remote_waits_and_is_reported_once_after_an_hour(tmp_path):
    host = FakeHost()

    def unreachable() -> str:
        raise deploy.GateUnknown("could not resolve host")

    host.fetch = unreachable
    for minute in range(0, 121, 2):
        assert deploy.run_once(host, out=tmp_path, now=1000.0 + 60 * minute) == 0
    assert host.calls == []
    assert host.notes == ["Deploy has waited 60 min: origin/main could not be fetched: could not resolve host."]
    assert state(tmp_path)["waiting"] == deploy.UNFETCHED


def test_a_resolved_fetch_wait_is_cleared_when_there_is_nothing_to_deploy(tmp_path):
    host = FakeHost()
    deploy.run_once(host, out=tmp_path, now=1000.0)
    real_fetch = host.fetch
    host.fetch = lambda: (_ for _ in ()).throw(deploy.GateUnknown("down"))
    deploy.run_once(host, out=tmp_path, now=1100.0)
    assert state(tmp_path)["waiting"] == deploy.UNFETCHED
    host.fetch = real_fetch
    deploy.run_once(host, out=tmp_path, now=1200.0)
    assert state(tmp_path)["waiting"] == ""


def test_an_unreadable_checkout_waits(tmp_path, capsys):
    host = FakeHost()

    def broken() -> str:
        raise deploy.GateUnknown("git status: index.lock exists")

    host.branch = broken
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert host.calls == []
    assert "the checkout could not be read: git status: index.lock exists" in capsys.readouterr().out


def test_a_crash_mid_deploy_is_a_failure_reported_once(tmp_path):
    host = FakeHost()

    def crash(log: Path) -> int:
        raise OSError("disk full")

    host.build = crash
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 1
    assert state(tmp_path)["failed"] == NEW
    assert len(host.notes) == 1 and "crashed: OSError: disk full" in host.notes[0]
    assert deploy.run_once(host, out=tmp_path, now=1120.0) == 0
    assert len(host.notes) == 1


def _failing_git(*args, **kwargs):
    return subprocess.CompletedProcess(args, 128, "", "fatal: index.lock exists")


@pytest.mark.parametrize("reader", ["head", "branch", "dirty_files"])
def test_a_failed_git_read_is_a_gate(monkeypatch, reader):
    host = deploy.RealHost(out=Path("out"), project="ordo")
    monkeypatch.setattr(deploy.subprocess, "run", _failing_git)
    with pytest.raises(deploy.GateUnknown, match="index.lock"):
        getattr(host, reader)()


def test_a_report_that_raises_never_loses_the_recorded_result(tmp_path):
    host = FakeHost()

    def broken(text: str) -> None:
        raise ValueError("unknown url type")

    host.notify = broken
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert state(tmp_path)["deployed"] == NEW
    host.calls.clear()
    assert deploy.run_once(host, out=tmp_path, now=1120.0) == 0
    assert host.calls == []                       # recorded, so not deployed again


def test_a_wait_report_that_raises_is_still_recorded_as_sent(tmp_path):
    host = FakeHost()
    host.leased = True
    sent = []

    def broken(text: str) -> None:
        sent.append(text)
        raise OSError("network down")

    host.notify = broken
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=1000.0 + deploy.WAIT_REPORT_SECONDS)
    deploy.run_once(host, out=tmp_path, now=1000.0 + deploy.WAIT_REPORT_SECONDS + 120)
    assert len(sent) == 1 and state(tmp_path)["waiting_reported"] is True


@pytest.mark.parametrize("url", ["discord.com/api/webhooks/1/x", "https://", "not a url at all"])
def test_the_real_notify_never_raises_on_a_bad_webhook(tmp_path, url):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / deploy.WEBHOOK_SECRET_FILE).write_text(url, encoding="utf-8")
    deploy.RealHost(out=tmp_path, project="ordo").notify("hello")


def test_a_failed_git_fetch_is_a_gate_not_a_crash(monkeypatch):
    host = deploy.RealHost(out=Path("out"), project="ordo")
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, "", "fatal: unable"))
    with pytest.raises(deploy.GateUnknown, match="unable"):
        host.fetch()


def test_a_new_commit_restarts_the_wait_clock(tmp_path):
    host = FakeHost()
    host.leased = True
    deploy.run_once(host, out=tmp_path, now=1000.0)
    host.target = "c" * 40
    deploy.run_once(host, out=tmp_path, now=1000.0 + deploy.WAIT_REPORT_SECONDS)
    assert host.notes == []
    assert state(tmp_path)["waiting"] == "c" * 40


def test_a_failed_build_is_reported_once_and_not_retried(tmp_path):
    host = FakeHost()
    host.build_code = 1
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 1
    assert host.calls == ["ff", "build"]
    assert state(tmp_path)["failed"] == NEW and state(tmp_path)["deployed"] == ""
    assert len(host.notes) == 1 and "`ordo build --all` failed" in host.notes[0]
    host.calls.clear()
    assert deploy.run_once(host, out=tmp_path, now=2000.0) == 0
    assert host.calls == [] and len(host.notes) == 1


def test_a_failed_apply_is_recorded_and_the_next_commit_deploys(tmp_path):
    host = FakeHost()
    host.apply_code = 1
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 1
    assert state(tmp_path)["failed"] == NEW
    host.apply_code = 0
    host.target = "c" * 40
    assert deploy.run_once(host, out=tmp_path, now=2000.0) == 0
    saved = state(tmp_path)
    assert saved["deployed"] == "c" * 40 and saved["failed"] == ""


def test_the_gates_are_read_again_after_the_build(tmp_path):
    host = FakeHost()
    host.busy_after_build = "CLI session(s): -z hello"
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert host.calls == ["ff", "build"]
    assert state(tmp_path)["waiting"] == NEW and state(tmp_path)["deployed"] == ""
    # The next pass, once the agent is idle, finishes the deploy (the checkout is already there).
    host.busy = None
    host.busy_after_build = None
    host.calls.clear()
    assert deploy.run_once(host, out=tmp_path, now=1200.0) == 0
    assert host.calls == ["ff", "build", "apply"]
    assert state(tmp_path)["deployed"] == NEW
    assert host.notes == [f"Deployed `{NEW[:12]}`: {OLD[:12]} -> {NEW[:12]}."]


def test_a_deploy_reports_the_commit_the_stack_ran_before(tmp_path):
    host = FakeHost()
    deploy.run_once(host, out=tmp_path, now=1000.0)
    host.target, host.notes = "c" * 40, []
    deploy.run_once(host, out=tmp_path, now=2000.0)
    assert host.notes == [f"Deployed `{'c' * 12}`: {NEW[:12]} -> {'c' * 12}."]


def test_the_crontab_line_quotes_its_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy.os, "name", "posix")
    repo, out = tmp_path / "my repo", tmp_path / "my out"
    assert deploy.install_schedule(out=out, project="ordo", repo=repo) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.split(" && ")[0].endswith(f'mkdir -p "{out}/deploy"')
    assert f'cd "{repo}"' in line and f'--out "{out}"' in line and f'>> "{out}/deploy/deploy.log"' in line


def test_the_lock_admits_one_pass_and_is_free_once_it_closes(tmp_path):
    lock = tmp_path / "deploy" / "lock"
    first = deploy.acquire_lock(lock)
    assert first is not None
    assert deploy.acquire_lock(lock) is None
    first.close()
    second = deploy.acquire_lock(lock)
    assert second is not None
    second.close()


HOLD_LOCK = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from ordo.host import deploy\n"
    "held = deploy.acquire_lock(Path(sys.argv[1]))\n"
    "print(held is not None, flush=True)\n"
    "time.sleep(60)\n"
)


def test_a_killed_pass_holds_no_lock(tmp_path):
    lock = tmp_path / "deploy" / "lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(lock)], stdout=subprocess.PIPE, text=True,
                              cwd=Path(deploy.__file__).resolve().parents[2])
    try:
        assert holder.stdout.readline().strip() == "True"
        assert deploy.acquire_lock(lock) is None
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
    held = deploy.acquire_lock(lock)
    assert held is not None
    held.close()


def test_an_apply_refusal_waits_and_is_tried_again(tmp_path):
    host = FakeHost()
    host.apply_code = deploy.APPLY_REFUSED
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert state(tmp_path)["waiting"] == NEW and state(tmp_path)["failed"] == ""
    assert host.notes == []
    host.apply_code = 0
    host.calls.clear()
    assert deploy.run_once(host, out=tmp_path, now=1120.0) == 0
    assert host.calls == ["ff", "build", "apply"]
    assert state(tmp_path)["deployed"] == NEW


# The real host's agent probe: what it reads from the container, and how it fails closed.

def _probe_host(monkeypatch, stdout: str, *, container: str | None = "ordo-agent-1"):
    monkeypatch.setattr(bringup, "find_running_container", lambda project, service: container)
    host = deploy.RealHost(out=Path("out"), project="ordo")
    monkeypatch.setattr(host, "_docker", lambda args: subprocess.CompletedProcess(args, 0, stdout, ""))
    return host


def test_the_agent_is_idle_with_no_turn_and_no_cli_session(monkeypatch):
    host = _probe_host(monkeypatch, json.dumps({"active_agents": 0, "cli_sessions": []}))
    assert host.agent_busy() is None


def test_the_agent_is_busy_with_a_turn_or_a_cli_session(monkeypatch):
    host = _probe_host(monkeypatch, json.dumps({"active_agents": 1, "cli_sessions": ["-z do a thing"]}))
    assert host.agent_busy() == "1 turn(s) in flight, CLI session(s): -z do a thing"


def test_no_agent_container_is_not_busy(monkeypatch):
    host = _probe_host(monkeypatch, "", container=None)
    assert host.agent_busy() is None


@pytest.mark.parametrize("stdout", ["", "not json", json.dumps({"cli_sessions": []}),
                                    json.dumps({"active_agents": None, "cli_sessions": []})])
def test_an_unreadable_agent_report_fails_closed(monkeypatch, stdout):
    host = _probe_host(monkeypatch, stdout)
    with pytest.raises(deploy.GateUnknown):
        host.agent_busy()


def test_the_probe_script_parses():
    compile(deploy._AGENT_BUSY_SCRIPT, "<agent-busy>", "exec")
