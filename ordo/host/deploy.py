"""`ordo deploy`: deploy origin/main whenever it moves, behind the gates a recreate must respect.

Every change to `main` is reviewed and merged on GitHub, so the deployed stack should follow `main`
without the operator running `ordo apply` by hand. A scheduled task on the host runs `ordo deploy`
every few minutes (`ordo deploy --install-schedule` registers it). Each run is one pass:

  1. fetch origin/main; nothing to do when it is the commit last deployed
  2. wait (and try again next run) unless every gate holds:
     - the checkout is on main with no uncommitted changes to tracked files, and fast-forwards to
       origin/main (a feature branch checked out here, or local edits, hold deploys)
     - changes made on main outside a pull request (local commits, or edits to tracked files) are
       rescued, never discarded: once they have stopped changing, they are scanned for secrets,
       pushed to a `deploy-rescue/` branch and opened as a pull request, so they are reviewed like
       any other change. Deploys hold while that pull request is open; once it is merged or closed
       the checkout is reset to origin/main (the changes stay on the pull request) and deploying
       resumes
     - no GPU lease is held
     - no one-off job container of the project is running (an eval in progress)
     - the default agent is idle: no turn in flight and no `hermes` CLI session running in it
  3. fast-forward the checkout, `ordo build --all`, check the gates again (a build takes minutes),
     then `ordo apply`, which ends with `ordo doctor`
  4. record the result in <out>/deploy/state.json and post it to the operator's Discord channel

A failed deploy is reported once and not retried: the fix is a new commit on main (a revert, or a
fix), which the next run deploys. A deploy that has waited an hour is reported once, with the gate
that holds it.

The second gate check narrows the window in which a turn can start before `ordo apply` recreates
the agent to the length of the apply itself (render and recreate; the builds are already done).

git, docker and the two `ordo` subcommands sit behind the `Host` seam, so the pass is tested
without them.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Protocol

from . import bringup

REPO_ROOT = Path(__file__).resolve().parents[2]
BRANCH = "main"
REMOTE = "origin"
AGENT_SERVICE = "agent"
# The Discord webhook Alertmanager posts to (`ordo secrets set ALERTMANAGER_DISCORD_WEBHOOK_URL`).
# Without it deploys still run; they are only recorded in the state file and the log.
WEBHOOK_SECRET_FILE = "alertmanager_discord_webhook_url"
WAIT_REPORT_SECONDS = 3600
# `ordo apply`'s exit code for a refusal before anything was recreated (a GPU lease taken after
# the gate check, or docker state it could not read): the deploy waits and is tried again.
APPLY_REFUSED = 2
SCHEDULED_TASK_NAME = "Ordo deploy"
SCHEDULE_MINUTES = 2
# What `waiting` holds when origin/main itself could not be fetched (no commit to name).
UNFETCHED = "origin/main (not fetched)"
# Changes made on the checkout outside a pull request are saved as one once they have been unchanged
# this long, so an edit in progress is not snapshotted half done.
RESCUE_SETTLE_SECONDS = 1800
RESCUE_BRANCH_PREFIX = "deploy-rescue/"
# Rescued changes are pushed to a public repo, so they are scanned first by the same TruffleHog
# release CI runs (.github/workflows/ci.yml, v3.88.26), stricter: unverified findings count too,
# because a false positive only holds the deploy while a missed secret would be published.
TRUFFLEHOG_IMAGE = ("trufflesecurity/trufflehog:3.88.26"
                    "@sha256:e306c1e5b522e74c94b7912d8bdeebe1dca26dfd2f55c7eb4be4707e5bd99997")
TRUFFLEHOG_FOUND = 183   # TruffleHog's exit code under --fail when it found something

# Run inside the agent container: the gateway's own count of turns in flight (written at every
# turn boundary) and every `hermes` CLI process other than the gateway and the dashboard
# (`hermes -z`, `hermes chat`, `hermes cron run`: sessions the gateway does not count).
_AGENT_BUSY_SCRIPT = r"""
import json, os
state = json.load(open("/opt/data/gateway_state.json"))
sessions = []
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) == os.getpid():  # this script names the marker itself
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    marks = [i for i, arg in enumerate(argv) if b"hermes_cli.main" in arg]
    if not marks:
        continue
    words = [arg.decode(errors="replace") for arg in argv[marks[0] + 1:] if arg]
    if words[:1] not in (["gateway"], ["dashboard"]):
        sessions.append(" ".join(words)[:80])
print(json.dumps({"active_agents": state.get("active_agents"), "cli_sessions": sessions}))
"""


class GateUnknown(Exception):
    """A gate could not be read. The pass waits, exactly as if the gate held."""


class Host(Protocol):
    def fetch(self) -> str: ...                       # origin/main's sha; GateUnknown when unreachable
    def head(self) -> str: ...
    def branch(self) -> str: ...
    def dirty_files(self) -> list[str]: ...           # uncommitted changes to tracked files
    def is_ancestor(self, older: str, newer: str) -> bool: ...
    def fast_forward(self, sha: str) -> bool: ...
    def gpu_leased(self) -> bool: ...                 # GateUnknown when it cannot be read
    def running_jobs(self) -> list[str]: ...          # GateUnknown when it cannot be read
    def agent_busy(self) -> str | None: ...           # why the agent is busy; GateUnknown when unreadable
    def build(self, log: Path) -> int: ...
    def apply(self, log: Path) -> int: ...
    def notify(self, text: str) -> None: ...
    # Rescuing changes made on the checkout outside a pull request. Each raises GateUnknown when it
    # cannot run, which makes the pass wait.
    def local_tree(self) -> str: ...                  # tree of HEAD plus uncommitted tracked changes
    def describe_local_changes(self, target: str) -> str: ...   # commits and files, for the PR body
    def save_local_changes(self, message: str) -> str: ...      # a commit of local_tree(), checkout untouched
    def secret_findings(self, base: str, sha: str) -> list[str]: ...   # what the scanner flagged
    def push_branch(self, sha: str, branch: str) -> None: ...    # force: the branch is the rescue's own
    def open_pr(self, branch: str, title: str, body: str) -> int: ...  # the PR number (reused if open)
    def pr_status(self, number: int) -> tuple[str, str]: ...     # (OPEN|MERGED|CLOSED, head commit)
    def reset_hard(self, sha: str) -> None: ...


@dataclasses.dataclass
class State:
    deployed: str = ""          # the last commit deployed successfully
    failed: str = ""            # the last commit whose deploy failed (reported, never retried)
    waiting: str = ""           # the commit waiting on a gate, and since when (unix seconds)
    waiting_since: float = 0.0
    waiting_reported: bool = False
    waiting_reason: str = ""
    deploying_from: str = ""     # the head before the fast-forward of the deploy in progress
    # Changes found on the checkout outside a pull request: their tree and since when unchanged, and
    # the pull request that holds them (its number, branch and head commit, and the tree it holds).
    local_tree: str = ""
    local_tree_since: float = 0.0
    rescue_pr: int = 0
    rescue_branch: str = ""
    rescue_sha: str = ""
    rescue_tree: str = ""
    rescue_blocked_tree: str = ""   # the tree whose secret-scan block was already reported
    last_result: str = ""
    last_log: str = ""
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        fields = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in doc.items() if key in fields})

    def save(self, path: Path) -> None:
        self.updated_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def short(sha: str) -> str:
    return sha[:12]


def blocking_gate(host: Host) -> str | None:
    """Why the stack cannot be recreated right now, or None when every runtime gate holds."""
    try:
        if host.gpu_leased():
            return "a GPU lease is held"
        jobs = host.running_jobs()
        if jobs:
            return f"a one-off job is running ({', '.join(jobs)})"
        busy = host.agent_busy()
        if busy:
            return f"the agent is busy ({busy})"
    except GateUnknown as e:
        return f"a gate could not be read: {e}"
    return None


def checkout_problem(host: Host, target: str) -> str | None:
    """Why the checkout cannot be fast-forwarded to `target`, or None."""
    branch = host.branch()
    if branch != BRANCH:
        return f"the checkout is on {branch}, not {BRANCH}"
    dirty = host.dirty_files()
    if dirty:
        shown = ", ".join(dirty[:5]) + (" ..." if len(dirty) > 5 else "")
        return f"the checkout has uncommitted changes ({shown})"
    if not host.is_ancestor(host.head(), target):
        return f"the checkout's {BRANCH} has commits that are not on {REMOTE}/{BRANCH}"
    return None


def has_local_changes(host: Host, target: str) -> bool:
    """Whether main carries work that is not on origin/main: commits, or uncommitted changes to
    tracked files. Another branch checked out is the operator's own doing and is left alone."""
    if host.branch() != BRANCH:
        return False
    return bool(host.dirty_files()) or not host.is_ancestor(host.head(), target)


def _clear_rescue(state: State) -> None:
    state.local_tree, state.local_tree_since = "", 0.0
    state.rescue_pr, state.rescue_branch, state.rescue_sha, state.rescue_tree = 0, "", "", ""
    state.rescue_blocked_tree = ""


def _rescue(host: Host, state: State, state_path: Path, target: str, now: float) -> str | None:
    """Move changes made on the checkout outside a pull request into one, and reset the checkout
    once that pull request is merged or closed. Returns why the deploy waits, or None when the
    checkout is back on origin/main."""
    tree = host.local_tree()
    if tree != state.local_tree:
        state.local_tree, state.local_tree_since = tree, now
        state.save(state_path)

    status, head = ("", "")
    if state.rescue_pr:
        status, head = host.pr_status(state.rescue_pr)
    if state.rescue_pr and tree == state.rescue_tree:
        if status == "OPEN":
            return (f"the checkout's changes made outside a pull request are in PR #{state.rescue_pr} "
                    f"({state.rescue_branch}); deploys hold until it is merged or closed")
        if head != state.rescue_sha:
            return (f"PR #{state.rescue_pr} is {status.lower()}, but its head is {short(head)}, not the "
                    f"rescued {short(state.rescue_sha)}; the checkout is left as it is")
        # The rescued changes are kept on GitHub (the pull request's head), so the checkout can go.
        number, branch = state.rescue_pr, state.rescue_branch
        host.reset_hard(target)
        _clear_rescue(state)
        state.save(state_path)
        print(f"PR #{number} is {status.lower()}: the checkout is reset to {REMOTE}/{BRANCH} ({short(target)})")
        _report(host, f"PR #{number} (changes rescued from the deploy checkout) is {status.lower()}: the "
                      f"checkout is back on `{REMOTE}/{BRANCH}`; the changes stay on `{branch}`.")
        return None

    if now - state.local_tree_since < RESCUE_SETTLE_SECONDS:
        return (f"the checkout has changes made outside a pull request; they are saved as one once "
                f"unchanged for {RESCUE_SETTLE_SECONDS // 60} min")

    sha = host.save_local_changes(
        "Changes found on the deploy checkout\n\n"
        "Made on the deploy checkout's main outside a pull request, and saved by `ordo deploy`.\n")
    findings = host.secret_findings(target, sha)
    if findings:
        reason = (f"the checkout's changes made outside a pull request were not pushed: the secret "
                  f"scanner flagged {', '.join(findings[:5])}; move them by hand")
        if state.rescue_blocked_tree != tree:
            state.rescue_blocked_tree = tree
            state.save(state_path)
            _report(host, f"Deploy holds: {reason}.")
        return reason

    # A pull request still open for earlier changes is updated in place; otherwise a new one.
    reuse = bool(state.rescue_pr) and status == "OPEN"
    # Named after the tree, so a pass that pushed but failed to open the pull request retries on the
    # same branch instead of leaving a new one behind each time.
    branch = state.rescue_branch if reuse else RESCUE_BRANCH_PREFIX + short(tree)
    host.push_branch(sha, branch)
    body = ("`ordo deploy` found these changes on the deploy checkout's `main`, made outside a pull "
            "request, and saved them here instead of discarding them. Deploys hold while this pull "
            "request is open.\n\n"
            "- **Merge** it to keep the changes.\n"
            "- **Close** it to drop them; they stay on this pull request.\n\n"
            "Either way the checkout is then reset to `origin/main` and deploying resumes.\n\n"
            + host.describe_local_changes(target))
    number = host.open_pr(branch, f"Changes found on the deploy checkout ({short(sha)})", body)
    state.rescue_pr, state.rescue_branch, state.rescue_sha, state.rescue_tree = number, branch, sha, tree
    state.rescue_blocked_tree = ""
    state.save(state_path)
    verb = "updated" if reuse else "opened"
    print(f"saved the checkout's changes as PR #{number} ({branch}, {verb})")
    _report(host, f"Changes made on the deploy checkout outside a pull request are saved as PR #{number} "
                  f"(`{branch}`, {verb}). Deploys hold until it is merged or closed.")
    return (f"the checkout's changes made outside a pull request are in PR #{number} ({branch}); "
            f"deploys hold until it is merged or closed")


def _wait(host: Host, state: State, state_path: Path, target: str, reason: str, now: float) -> int:
    if state.waiting != target:
        state.waiting, state.waiting_since, state.waiting_reported = target, now, False
        state.waiting_reason = ""
    subject = "Deploy" if target == UNFETCHED else f"Deploy of `{short(target)}`"
    # Printed when the reason changes, not on every pass: a wait can last hours, two minutes apart.
    if reason != state.waiting_reason:
        print(f"{subject} waiting: {reason}")
        state.waiting_reason = reason
    report = not state.waiting_reported and now - state.waiting_since >= WAIT_REPORT_SECONDS
    if report:
        state.waiting_reported = True
    # Saved before reporting: a report that fails must never lose the recorded state.
    state.save(state_path)
    if report:
        minutes = int((now - state.waiting_since) // 60)
        _report(host, f"{subject} has waited {minutes} min: {reason}.")
    return 0


def _finish(host: Host, state: State, state_path: Path, target: str, *, ok: bool, result: str,
            log: Path) -> int:
    state.waiting, state.waiting_since, state.waiting_reported, state.waiting_reason = "", 0.0, False, ""
    state.deploying_from = ""
    state.last_result, state.last_log = result, str(log)
    if ok:
        state.deployed, state.failed = target, ""
    else:
        state.failed = target
    # Saved before reporting: a report that fails must never turn a recorded result into a retry.
    state.save(state_path)
    print(f"deploy of {short(target)}: {result}")
    if ok:
        _report(host, f"Deployed `{short(target)}`: {result}.")
    else:
        _report(host, f"Deploy of `{short(target)}` failed: {result}. Log: `{log}`. "
                      f"Not retried; the next commit on {BRANCH} deploys.")
    return 0 if ok else 1


def _report(host: Host, text: str) -> None:
    """Post to the operator's channel. Reporting is best effort: its failure is logged, never raised."""
    try:
        host.notify(text)
    except Exception as e:  # noqa: BLE001 - a broken webhook must not break the deploy's bookkeeping
        print(f"could not post the report: {type(e).__name__}: {e}", file=sys.stderr)


def run_once(host: Host, *, out: Path, now: float | None = None) -> int:
    """One pass: deploy origin/main if it moved and every gate holds. 0 = deployed, nothing to do,
    or waiting; 1 = the deploy failed."""
    now = time.time() if now is None else now
    deploy_dir = out / "deploy"
    state_path = deploy_dir / "state.json"
    state = State.load(state_path)

    try:
        target = host.fetch()
    except GateUnknown as e:
        return _wait(host, state, state_path, UNFETCHED, f"origin/{BRANCH} could not be fetched: {e}", now)
    if target in (state.deployed, state.failed):
        if state.waiting:   # a wait (an unreachable remote, say) that resolved into nothing to do
            state.waiting, state.waiting_since, state.waiting_reported, state.waiting_reason = "", 0.0, False, ""
            state.save(state_path)
        return 0

    try:
        problem = checkout_problem(host, target)
        if problem and has_local_changes(host, target):
            problem = _rescue(host, state, state_path, target, now) or checkout_problem(host, target)
    except GateUnknown as e:
        problem = f"the checkout could not be read: {e}"
    problem = problem or blocking_gate(host)
    if problem:
        return _wait(host, state, state_path, target, problem, now)

    stamp = dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    log = deploy_dir / "logs" / f"{stamp}-{short(target)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _deploy(host, state, state_path, target, log, now)
    except Exception as e:  # noqa: BLE001 - a crash mid-deploy is a failed deploy, reported once
        return _finish(host, state, state_path, target, ok=False, result=f"crashed: {type(e).__name__}: {e}",
                       log=log)


def _deploy(host: Host, state: State, state_path: Path, target: str, log: Path, now: float) -> int:
    """Fast-forward, build, re-check the gates, apply."""
    # What the stack ran before this deploy: the last commit deployed, else the head before the
    # fast-forward (saved, because a pass that waits after its build has already fast-forwarded).
    previous = state.deployed or state.deploying_from or host.head()
    state.deploying_from = previous
    state.save(state_path)
    if not host.fast_forward(target):
        return _finish(host, state, state_path, target, ok=False, result="the fast-forward failed", log=log)
    if host.build(log) != 0:
        return _finish(host, state, state_path, target, ok=False, result="`ordo build --all` failed", log=log)
    # The build took minutes: a lease, an eval or a turn may have started since the first check.
    problem = blocking_gate(host)
    if problem:
        return _wait(host, state, state_path, target, problem, now)
    code = host.apply(log)
    if code == APPLY_REFUSED:
        return _wait(host, state, state_path, target,
                     f"`ordo apply` refused before recreating anything (log: {log})", now)
    if code != 0:
        return _finish(host, state, state_path, target, ok=False, result="`ordo apply` failed", log=log)
    return _finish(host, state, state_path, target, ok=True, result=f"{short(previous)} -> {short(target)}",
                   log=log)


# --------------------------------------------------------------------------- #
# The real host.
# --------------------------------------------------------------------------- #


class RealHost:
    def __init__(self, *, out: Path, project: str, repo: Path = REPO_ROOT):
        self.out = Path(out)
        self.project = project
        self.repo = repo

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run git in the checkout. GateUnknown when it cannot run, or (with `check`) fails: a
        checkout git cannot read (a stale index.lock, say) holds the deploy like any gate."""
        try:
            proc = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                                  timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            raise GateUnknown(f"git {args[0]}: {e}") from e
        if check and proc.returncode != 0:
            raise GateUnknown(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def fetch(self) -> str:
        self._git("fetch", "--quiet", REMOTE, BRANCH)
        return self._git("rev-parse", f"{REMOTE}/{BRANCH}").stdout.strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def dirty_files(self) -> list[str]:
        lines = self._git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
        return [line[3:] for line in lines if line.strip()]

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def fast_forward(self, sha: str) -> bool:
        return self._git("merge", "--ff-only", "--quiet", sha, check=False).returncode == 0

    def local_tree(self) -> str:
        # `git stash create` writes a commit of the index and working tree (tracked files only)
        # without touching either, and prints nothing when there is nothing uncommitted.
        stash = self._git("stash", "create").stdout.strip()
        return self._git("rev-parse", f"{stash or 'HEAD'}^{{tree}}").stdout.strip()

    def describe_local_changes(self, target: str) -> str:
        commits = self._git("log", "--format=- %h %an: %s", f"{target}..HEAD").stdout.strip()
        dirty = self.dirty_files()
        parts = [f"**Commits not on `{REMOTE}/{BRANCH}`:**\n{commits}" if commits else "",
                 "**Uncommitted changes:**\n" + "\n".join(f"- `{name}`" for name in dirty) if dirty else ""]
        return "\n\n".join(part for part in parts if part)

    def save_local_changes(self, message: str) -> str:
        tree = self.local_tree()
        if tree == self._git("rev-parse", "HEAD^{tree}").stdout.strip():
            return self.head()   # only local commits: the head already holds them
        return self._git("commit-tree", tree, "-p", "HEAD", "-m", message).stdout.strip()

    def secret_findings(self, base: str, sha: str) -> list[str]:
        scan = self.out / "deploy" / "secret-scan"
        scan.mkdir(parents=True, exist_ok=True)
        (scan / "changes.diff").write_text(self._git("diff", base, sha).stdout, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "-v", f"{scan.resolve()}:/scan:ro", TRUFFLEHOG_IMAGE,
                 "filesystem", "/scan", "--results=verified,unknown,unverified", "--exclude-detectors=lob",
                 "--no-verification", "--no-update", "--fail", "--json"],
                capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            raise GateUnknown(f"the secret scanner could not run: {e}") from e
        finally:
            (scan / "changes.diff").unlink(missing_ok=True)
        if proc.returncode == 0:
            return []
        if proc.returncode != TRUFFLEHOG_FOUND:
            raise GateUnknown(f"the secret scanner failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
        found = []
        for line in proc.stdout.splitlines():
            try:
                found.append(json.loads(line).get("DetectorName") or "a secret")
            except json.JSONDecodeError:
                continue
        return sorted(set(found)) or ["a secret"]

    def push_branch(self, sha: str, branch: str) -> None:
        if not branch.startswith(RESCUE_BRANCH_PREFIX):
            raise ValueError(f"refusing to push {branch}: only {RESCUE_BRANCH_PREFIX} branches are the rescue's own")
        self._git("push", "--quiet", "--force", REMOTE, f"{sha}:refs/heads/{branch}")

    def _gh(self, *args: str, stdin: str | None = None) -> str:
        try:
            proc = subprocess.run(["gh", *args], cwd=self.repo, input=stdin, capture_output=True, text=True,
                                  timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise GateUnknown(f"gh {args[0]}: {e}") from e
        if proc.returncode != 0:
            raise GateUnknown(f"gh {' '.join(args[:2])}: {(proc.stderr or proc.stdout).strip()[:200]}")
        return proc.stdout.strip()

    def open_pr(self, branch: str, title: str, body: str) -> int:
        existing = self._gh("pr", "list", "--head", branch, "--state", "open", "--json", "number",
                            "--jq", ".[0].number // empty")
        if existing:
            return int(existing)
        url = self._gh("pr", "create", "--base", BRANCH, "--head", branch, "--title", title,
                       "--body-file", "-", stdin=body)
        try:
            return int(url.rstrip("/").rsplit("/", 1)[1])
        except (IndexError, ValueError) as e:
            raise GateUnknown(f"gh pr create printed no pull request URL: {url[:200]}") from e

    def pr_status(self, number: int) -> tuple[str, str]:
        doc = self._gh("pr", "view", str(number), "--json", "state,headRefOid")
        try:
            report = json.loads(doc)
            return str(report["state"]), str(report["headRefOid"])
        except (ValueError, KeyError) as e:
            raise GateUnknown(f"gh pr view {number}: unreadable: {doc[:200]}") from e

    def reset_hard(self, sha: str) -> None:
        self._git("reset", "--hard", "--quiet", sha)

    def gpu_leased(self) -> bool:
        try:
            gpu = bringup.read_gpu_status(self.project)
            return gpu is not None and bringup.is_leased(gpu)
        except bringup.LeaseUnknown as e:
            raise GateUnknown(str(e)) from e

    def running_jobs(self) -> list[str]:
        proc = self._docker(["ps", "--filter", f"label=com.docker.compose.project={self.project}",
                             "--filter", "label=com.docker.compose.oneoff=True",
                             "--filter", "status=running", "--format", "{{.Names}}"])
        return proc.stdout.split()

    def agent_busy(self) -> str | None:
        try:
            container = bringup.find_running_container(self.project, AGENT_SERVICE)
        except bringup.LeaseUnknown as e:
            raise GateUnknown(str(e)) from e
        if container is None:
            return None
        proc = self._docker(["exec", container, "python3", "-c", _AGENT_BUSY_SCRIPT])
        try:
            report = json.loads(proc.stdout)
            active = int(report["active_agents"])
            sessions = list(report["cli_sessions"])
        except (ValueError, KeyError, TypeError) as e:
            raise GateUnknown(f"the agent's activity is unreadable: {proc.stdout.strip()[:200]}") from e
        reasons = []
        if active:
            reasons.append(f"{active} turn(s) in flight")
        if sessions:
            reasons.append(f"CLI session(s): {'; '.join(sessions)}")
        return ", ".join(reasons) or None

    def _docker(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise GateUnknown(f"docker {args[0]}: {e}") from e
        if proc.returncode != 0:
            raise GateUnknown(f"docker {args[0]}: {(proc.stderr or proc.stdout).strip()[:200]}")
        return proc

    def _ordo(self, args: list[str], log: Path) -> int:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\n$ ordo {' '.join(args)}\n")
            stream.flush()
            return subprocess.run([sys.executable, "-m", "ordo", *args, "--out", str(self.out),
                                   "--project", self.project],
                                  cwd=self.repo, stdout=stream, stderr=subprocess.STDOUT).returncode

    def build(self, log: Path) -> int:
        return self._ordo(["build", "--all"], log)

    def apply(self, log: Path) -> int:
        return self._ordo(["apply"], log)

    def notify(self, text: str) -> None:
        secret = self.out / "secrets" / WEBHOOK_SECRET_FILE
        try:
            url = secret.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return
        if not url:
            return
        try:
            # Built inside the try: a URL urllib cannot parse (no scheme) raises ValueError here.
            request = urllib.request.Request(url, data=json.dumps({"content": f"**Ordo deploy** {text}"}).encode(),
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "ordo-deploy"})
            urllib.request.urlopen(request, timeout=20).close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"could not post to Discord: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# The lock and the schedule.
# --------------------------------------------------------------------------- #


def acquire_lock(path: Path) -> IO[str] | None:
    """An exclusive OS lock on `path`, held until the returned file is closed, or None when another
    pass holds it. The OS drops the lock when its process dies (Task Scheduler ends a pass at its
    time limit), so a dead pass never blocks the next one. The file itself stays."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def install_schedule(*, out: Path, project: str, repo: Path = REPO_ROOT) -> int:
    """Register the scheduled task that runs `ordo deploy` every SCHEDULE_MINUTES (Windows), or
    print the crontab line to add (elsewhere). Re-running replaces the task."""
    python = Path(sys.executable)
    args = f"-m ordo deploy --out \"{out}\" --project {project}"
    if os.name != "nt":
        print(f"add to the operator's crontab (crontab -e):\n"
              f"*/{SCHEDULE_MINUTES} * * * * mkdir -p \"{out}/deploy\" && cd \"{repo}\" && \"{python}\" {args} "
              f">> \"{out}/deploy/deploy.log\" 2>&1")
        return 0
    # `conhost --headless` gives the pass a console no window shows, which every git and docker
    # child inherits (a console-less pythonw would open a window for each of them). The task runs
    # as the operator while they are signed in, which is when Docker Desktop runs; registering it
    # needs no elevation and stores no password.
    # The paths reach PowerShell as environment variables, never spliced into its source, so no
    # character in them (an apostrophe, say) can break the script.
    log = Path(out) / "deploy" / "deploy.log"
    env = dict(os.environ, ORDO_TASK_ARGUMENT=f'--headless "{python}" {args} --log "{log}"',
               ORDO_TASK_DIRECTORY=str(repo), ORDO_TASK_NAME=SCHEDULED_TASK_NAME)
    script = f"""
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $env:ORDO_TASK_ARGUMENT -WorkingDirectory $env:ORDO_TASK_DIRECTORY
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes {SCHEDULE_MINUTES})
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $env:ORDO_TASK_NAME -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"could not register the scheduled task: {(proc.stderr or proc.stdout).strip()}", file=sys.stderr)
        return 1
    print(f"registered scheduled task '{SCHEDULED_TASK_NAME}': `ordo deploy` every {SCHEDULE_MINUTES} min, "
          f"log {log}")
    return 0


def print_status(out: Path) -> int:
    state = State.load(Path(out) / "deploy" / "state.json")
    print(json.dumps(dataclasses.asdict(state), indent=2))
    return 0
