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
        # Rescue: the checkout's tree, what the scanner flags, and the pull requests opened.
        self.tree = "t1"
        self.replayed: str | None = "r1"          # local changes merged onto origin/main (None = conflict)
        self.findings: list[str] = []
        self.prs: dict[int, list] = {}            # number -> [state, head, branch]
        self.history: dict[int, set[str]] = {}    # further commits each pull request's history holds
        self.commit_prs: dict[str, tuple[int, str]] = {}   # local commit -> (PR, state) holding it
        self.pushes: list[tuple[str, str, str]] = []
        self.saved = 0
        self.open_pr_fails = False

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

    def local_tree(self) -> str:
        return self.tree

    def replayed_tree(self, target: str) -> str | None:
        return self.replayed

    def target_tree(self, target: str) -> str:
        return "target-tree"

    def describe_local_changes(self, target: str) -> str:
        return "**Uncommitted changes:**\n- `services/comfyui/plugin.yaml`"

    def commit_onto(self, tree: str, target: str, message: str) -> str:
        self.saved += 1
        return f"rescue-{self.tree}"

    def secret_findings(self, base: str, sha: str) -> list[str]:
        self.calls.append("scan")
        return self.findings

    def pr_for_commit(self, sha: str) -> tuple[int, str] | None:
        return self.commit_prs.get(sha)

    def push_branch(self, sha: str, branch: str, expected: str) -> None:
        # What the branch holds on the remote: its PR's head (a fixer may have pushed), else the
        # last push to it, else nothing.
        pushed = [p[0] for p in self.pushes if p[1] == branch]
        current = next((pr[1] for pr in self.prs.values() if pr[2] == branch), pushed[-1] if pushed else "")
        if current != expected:
            raise deploy.GateUnknown(f"git push: stale info ({branch} is {current or 'absent'})")
        self.pushes.append((sha, branch, expected))
        for pr in self.prs.values():
            if pr[2] == branch and pr[0] == "OPEN":
                pr[1] = sha

    def open_pr(self, branch: str, title: str, body: str) -> int:
        if self.open_pr_fails:
            raise deploy.GateUnknown("gh pr: HTTP 502")
        for number, pr in self.prs.items():
            if pr[2] == branch and pr[0] == "OPEN":
                return number
        number = 500 + len(self.prs)
        self.prs[number] = ["OPEN", self.pushes[-1][0], branch]
        return number

    def pr_status(self, number: int) -> tuple[str, str]:
        return self.prs[number][0], self.prs[number][1]

    def pr_contains(self, number: int, sha: str) -> bool:
        return sha == self.prs[number][1] or sha in self.history.get(number, set())

    def reset_hard(self, sha: str) -> None:
        self.calls.append("reset")
        self.head_sha, self.dirty, self.ancestor = sha, [], True


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
    (lambda h: setattr(h, "dirty", ["ordo/cli.py"]), "the checkout has changes made outside a pull request"),
    (lambda h: setattr(h, "ancestor", False), "the checkout has changes made outside a pull request"),
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


# --------------------------------------------------------------------------- #
# Changes made on the checkout outside a pull request are rescued into one.
# --------------------------------------------------------------------------- #
SETTLED = 1000.0 + deploy.RESCUE_SETTLE_SECONDS
BRANCH1 = "deploy-rescue/19700101T004640Z"          # named at SETTLED


def edited_host() -> FakeHost:
    host = FakeHost()
    host.dirty = ["services/comfyui/plugin.yaml"]
    return host


def rescued(tmp_path) -> FakeHost:
    host = edited_host()
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=SETTLED)
    return host


def test_local_changes_are_left_alone_until_they_stop_changing(tmp_path, capsys):
    host = edited_host()
    deploy.run_once(host, out=tmp_path, now=1000.0)
    host.tree = "t2"                                # still being edited
    deploy.run_once(host, out=tmp_path, now=SETTLED)
    assert host.saved == 0 and host.pushes == [] and host.calls == []
    assert "saved as one once unchanged for 30 min" in capsys.readouterr().out


def test_settled_local_changes_become_one_pull_request_and_deploys_hold(tmp_path, capsys):
    host = rescued(tmp_path)
    assert host.pushes == [("rescue-t1", BRANCH1, "")]      # a new branch: must not exist yet
    assert host.prs == {500: ["OPEN", "rescue-t1", BRANCH1]}
    assert host.calls == ["scan"]                   # scanned, nothing deployed, nothing reset
    saved = state(tmp_path)
    assert (saved["rescue_pr"], saved["rescue_sha"], saved["rescue_tree"]) == (500, "rescue-t1", "t1")
    assert host.notes == ["Changes made on the deploy checkout outside a pull request are saved as PR #500 "
                          f"(`{BRANCH1}`, opened). Deploys hold until it is merged or closed."]
    assert "are in PR #500" in capsys.readouterr().out


def test_an_open_rescue_is_not_pushed_or_scanned_again(tmp_path):
    host = rescued(tmp_path)
    for minutes in range(2, 300, 2):
        deploy.run_once(host, out=tmp_path, now=SETTLED + 60 * minutes)
    assert len(host.pushes) == 1 and host.calls == ["scan"]
    assert len(host.notes) == 2 and "has waited" in host.notes[1]


@pytest.mark.parametrize("outcome", ["MERGED", "CLOSED"])
def test_a_finished_rescue_resets_the_checkout_and_deploying_resumes(tmp_path, outcome):
    host = rescued(tmp_path)
    host.prs[500][0] = outcome
    assert deploy.run_once(host, out=tmp_path, now=SETTLED + 120) == 0
    assert host.calls == ["scan", "reset", "ff", "build", "apply"]
    saved = state(tmp_path)
    assert saved["deployed"] == NEW and saved["rescue_pr"] == 0 and saved["local_tree"] == ""
    assert host.notes[1] == (f"PR #500 (changes rescued from the deploy checkout) is {outcome.lower()}: the "
                             f"checkout is back on `origin/main`; the changes stay on `{BRANCH1}`.")


def test_a_rescue_merged_after_a_review_fix_on_top_is_reset(tmp_path):
    host = rescued(tmp_path)
    host.prs[500][:2] = ["MERGED", "fix-on-top"]
    host.history[500] = {"rescue-t1"}
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    assert "reset" in host.calls and host.calls[-1] == "apply"


def test_a_rescue_whose_pull_request_lost_the_commit_is_never_reset(tmp_path, capsys):
    host = rescued(tmp_path)
    host.prs[500][:2] = ["CLOSED", "someone-else"]
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    assert "reset" not in host.calls
    assert "its history no longer holds the rescued rescue-t1 (head someone-else)" in capsys.readouterr().out


def test_newer_edits_update_an_untouched_open_pull_request(tmp_path):
    host = rescued(tmp_path)
    host.tree = "t2"
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    assert len(host.pushes) == 1                    # settling again first
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120 + deploy.RESCUE_SETTLE_SECONDS)
    assert host.pushes[-1] == ("rescue-t2", BRANCH1, "rescue-t1")     # only over the rescue's own commit
    assert list(host.prs) == [500] and host.prs[500][1] == "rescue-t2"
    assert any(f"(`{BRANCH1}`, updated)" in note for note in host.notes) and "reset" not in host.calls


def test_newer_edits_never_push_over_a_review_fix(tmp_path):
    host = rescued(tmp_path)
    host.prs[500][1] = "fix-on-top"                 # still open, a fixer pushed
    host.tree = "t2"
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120 + deploy.RESCUE_SETTLE_SECONDS)
    assert host.prs[500][1] == "fix-on-top"
    assert sorted(host.prs) == [500, 501] and host.prs[501][2] != BRANCH1


def test_a_push_made_in_between_is_never_overwritten(tmp_path, capsys):
    host = rescued(tmp_path)
    host.tree = "t2"
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    real_status = host.pr_status

    def status_then_fixer_pushes(number):           # a fixer pushes right after the status read
        result = real_status(number)
        host.prs[500][1] = "fix-on-top"
        return result

    host.pr_status = status_then_fixer_pushes
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120 + deploy.RESCUE_SETTLE_SECONDS)
    assert host.prs[500][1] == "fix-on-top"
    assert "stale info" in capsys.readouterr().out


def test_a_pull_request_that_failed_to_open_is_opened_next_pass_without_a_new_push(tmp_path):
    host = edited_host()
    host.open_pr_fails = True
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=SETTLED)
    assert len(host.pushes) == 1 and host.prs == {} and state(tmp_path)["rescue_branch"] == BRANCH1
    host.open_pr_fails = False
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    assert len(host.pushes) == 1 and list(host.prs) == [500]
    assert state(tmp_path)["rescue_pr"] == 500


def test_changes_after_a_closed_rescue_get_a_new_pull_request(tmp_path):
    host = rescued(tmp_path)
    host.prs[500][0] = "CLOSED"
    host.tree = "t2"
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120 + deploy.RESCUE_SETTLE_SECONDS)
    assert "reset" not in host.calls
    assert sorted(host.prs) == [500, 501] and host.prs[501][1] == "rescue-t2"


def test_changes_the_secret_scanner_flags_are_never_pushed_and_scanned_once(tmp_path):
    host = edited_host()
    host.findings = ["Github"]
    deploy.run_once(host, out=tmp_path, now=1000.0)
    for minutes in range(0, 20, 2):
        deploy.run_once(host, out=tmp_path, now=SETTLED + 60 * minutes)
    assert host.pushes == [] and host.prs == {} and host.calls == ["scan"]
    assert host.notes == ["Deploy holds: the checkout's changes made outside a pull request were not pushed: "
                          "the secret scanner flagged Github; move them by hand."]


def test_changes_that_conflict_with_origin_main_are_reported_once(tmp_path):
    host = edited_host()
    host.replayed = None
    deploy.run_once(host, out=tmp_path, now=1000.0)
    for minutes in range(0, 10, 2):
        deploy.run_once(host, out=tmp_path, now=SETTLED + 60 * minutes)
    assert host.pushes == [] and host.calls == []
    assert len(host.notes) == 1 and "conflict with origin/main" in host.notes[0]


def test_local_changes_already_on_origin_main_are_reset_at_once(tmp_path, capsys):
    host = FakeHost()
    host.ancestor = False                           # a local commit, squash-merged on GitHub since
    host.replayed = "target-tree"
    assert deploy.run_once(host, out=tmp_path, now=1000.0) == 0
    assert host.calls == ["reset", "ff", "build", "apply"] and host.pushes == []
    assert "already on origin/main" in capsys.readouterr().out


@pytest.mark.parametrize("pr_state, resets", [("OPEN", False), ("MERGED", True), ("CLOSED", True)])
def test_a_local_commit_a_pull_request_holds_follows_that_pull_request(tmp_path, capsys, pr_state, resets):
    host = FakeHost()
    host.ancestor = False                           # an agent committed here and opened PR #386 from it
    host.replayed = None                            # the PR's later review fix conflicts with it
    host.commit_prs[OLD] = (386, pr_state)
    deploy.run_once(host, out=tmp_path, now=1000.0)
    assert host.pushes == [] and host.saved == 0
    assert ("reset" in host.calls) is resets
    if not resets:
        assert "a commit of PR #386" in capsys.readouterr().out


def test_a_rescue_is_forgotten_when_the_checkout_is_cleaned_by_hand(tmp_path):
    host = rescued(tmp_path)
    host.dirty = []                                 # the operator reset it themselves
    deploy.run_once(host, out=tmp_path, now=SETTLED + 120)
    saved = state(tmp_path)
    assert saved["rescue_pr"] == 0 and saved["rescue_branch"] == "" and saved["local_tree"] == ""


def test_another_branch_checked_out_is_never_rescued(tmp_path):
    host = FakeHost(branch="feat/x")
    host.dirty = ["ordo/cli.py"]
    deploy.run_once(host, out=tmp_path, now=1000.0)
    deploy.run_once(host, out=tmp_path, now=SETTLED)
    assert host.saved == 0 and host.pushes == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "plugin.yaml").write_text("pin: old\n", encoding="utf-8")
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo


def _advance(repo: Path, branch: str, name: str, text: str) -> str:
    """A commit on `branch` (origin/main stand-in) that the checkout's main does not have."""
    _git(repo, "checkout", "--quiet", "-B", branch)
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", f"{branch}: {name}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--quiet", "main")
    return sha


def test_the_rescue_commit_is_one_commit_on_origin_main_and_the_checkout_is_untouched(repo, tmp_path):
    host = deploy.RealHost(out=tmp_path / "out", project="ordo", repo=repo)
    base = host.head()
    target = _advance(repo, "remote-main", "notes.md", "notes, newer on main\n")
    (repo / "plugin.yaml").write_text("pin: secret-then-removed\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "local commit one")           # local history ...
    (repo / "plugin.yaml").write_text("pin: new\n", encoding="utf-8")   # ... then an edit on top
    before = _git(repo, "status", "--porcelain")
    sha = host.commit_onto(host.replayed_tree(target), target, "rescue")
    assert _git(repo, "status", "--porcelain") == before == "M plugin.yaml"
    assert _git(repo, "rev-parse", "HEAD~1") == base                      # main did not move
    assert _git(repo, "rev-parse", f"{sha}^") == target                  # one commit, parent origin/main
    assert _git(repo, "rev-list", "--count", f"{target}..{sha}") == "1"   # no local history goes out
    assert _git(repo, "show", f"{sha}:plugin.yaml") == "pin: new"
    assert _git(repo, "show", f"{sha}:notes.md") == "notes, newer on main"   # main's change kept
    assert "local commit one" in host.describe_local_changes(target)


def test_replayed_tree_reports_conflicts_and_changes_already_on_main(repo, tmp_path):
    host = deploy.RealHost(out=tmp_path / "out", project="ordo", repo=repo)
    same = _advance(repo, "same-main", "plugin.yaml", "pin: new\n")
    other = _advance(repo, "other-main", "plugin.yaml", "pin: other\n")
    (repo / "plugin.yaml").write_text("pin: new\n", encoding="utf-8")      # the local edit
    assert host.replayed_tree(same) == host.target_tree(same)              # already on main
    assert host.replayed_tree(other) is None                              # a conflict


def test_a_diff_the_windows_code_page_cannot_decode_is_read(repo, tmp_path):
    host = deploy.RealHost(out=tmp_path / "out", project="ordo", repo=repo)
    # U+201D is E2 80 9D in UTF-8; 0x9D has no cp1252 mapping, so a cp1252 read of it crashed the pass.
    (repo / "notes.md").write_text("quote \u201d and \u2014 dash\n", encoding="utf-8")
    assert "\u201d" in host._git("diff", "HEAD").stdout
    assert "notes.md" in host.describe_local_changes(host.head())


def test_the_rescue_pushes_only_its_own_branches(tmp_path):
    host = deploy.RealHost(out=tmp_path, project="ordo")
    with pytest.raises(ValueError, match="deploy-rescue/"):
        host.push_branch("abc", "main", "")


def test_the_secret_scanner_is_the_release_ci_runs():
    ci = (Path(deploy.__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = deploy.TRUFFLEHOG_IMAGE.split(":")[1].split("@")[0]
    assert "trufflesecurity/trufflehog@" in ci and f"# v{version}" in ci
    assert "@sha256:" in deploy.TRUFFLEHOG_IMAGE
