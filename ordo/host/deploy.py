"""`ordo deploy`: deploy origin/main whenever it moves, behind the gates a recreate must respect.

Every change to `main` is reviewed and merged on GitHub, so the deployed stack should follow `main`
without the operator running `ordo apply` by hand. A scheduled task on the host runs `ordo deploy`
every few minutes (`ordo deploy --install-schedule` registers it). Each run is one pass:

  1. fetch origin/main; nothing to do when it is the commit last deployed
  2. wait (and try again next run) unless every gate holds:
     - the checkout is on main with no uncommitted changes to tracked files, and fast-forwards to
       origin/main (a feature branch checked out here, or local edits, hold deploys)
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
from typing import Protocol

from . import bringup

REPO_ROOT = Path(__file__).resolve().parents[2]
BRANCH = "main"
REMOTE = "origin"
AGENT_SERVICE = "agent"
# The Discord webhook Alertmanager posts to (`ordo secrets set ALERTMANAGER_DISCORD_WEBHOOK_URL`).
# Without it deploys still run; they are only recorded in the state file and the log.
WEBHOOK_SECRET_FILE = "alertmanager_discord_webhook_url"
WAIT_REPORT_SECONDS = 3600
# A lock older than this belongs to a run that died without removing it.
STALE_LOCK_SECONDS = 3 * 3600
SCHEDULED_TASK_NAME = "Ordo deploy"
SCHEDULE_MINUTES = 2

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
    def fetch(self) -> str: ...                       # origin/main's sha after a fetch
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


@dataclasses.dataclass
class State:
    deployed: str = ""          # the last commit deployed successfully
    failed: str = ""            # the last commit whose deploy failed (reported, never retried)
    waiting: str = ""           # the commit waiting on a gate, and since when (unix seconds)
    waiting_since: float = 0.0
    waiting_reported: bool = False
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


def _wait(host: Host, state: State, state_path: Path, target: str, reason: str, now: float) -> int:
    if state.waiting != target:
        state.waiting, state.waiting_since, state.waiting_reported = target, now, False
    print(f"deploy of {short(target)} waiting: {reason}")
    if not state.waiting_reported and now - state.waiting_since >= WAIT_REPORT_SECONDS:
        minutes = int((now - state.waiting_since) // 60)
        host.notify(f"Deploy of `{short(target)}` has waited {minutes} min: {reason}.")
        state.waiting_reported = True
    state.save(state_path)
    return 0


def _finish(host: Host, state: State, state_path: Path, target: str, *, ok: bool, result: str,
            log: Path) -> int:
    state.waiting, state.waiting_since, state.waiting_reported = "", 0.0, False
    state.last_result, state.last_log = result, str(log)
    if ok:
        state.deployed, state.failed = target, ""
        host.notify(f"Deployed `{short(target)}`: {result}.")
    else:
        state.failed = target
        host.notify(f"Deploy of `{short(target)}` failed: {result}. Log: `{log}`. "
                    f"Not retried; the next commit on {BRANCH} deploys.")
    state.save(state_path)
    print(f"deploy of {short(target)}: {result}")
    return 0 if ok else 1


def run_once(host: Host, *, out: Path, now: float | None = None) -> int:
    """One pass: deploy origin/main if it moved and every gate holds. 0 = deployed, nothing to do,
    or waiting; 1 = the deploy failed."""
    now = time.time() if now is None else now
    deploy_dir = out / "deploy"
    state_path = deploy_dir / "state.json"
    state = State.load(state_path)

    target = host.fetch()
    if target in (state.deployed, state.failed):
        return 0

    problem = checkout_problem(host, target) or blocking_gate(host)
    if problem:
        return _wait(host, state, state_path, target, problem, now)

    stamp = dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    log = deploy_dir / "logs" / f"{stamp}-{short(target)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    previous = host.head()
    if not host.fast_forward(target):
        return _finish(host, state, state_path, target, ok=False, result="the fast-forward failed", log=log)
    if host.build(log) != 0:
        return _finish(host, state, state_path, target, ok=False, result="`ordo build --all` failed", log=log)
    # The build took minutes: a lease, an eval or a turn may have started since the first check.
    problem = blocking_gate(host)
    if problem:
        return _wait(host, state, state_path, target, problem, now)
    if host.apply(log) != 0:
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
        proc = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=300)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
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
        request = urllib.request.Request(url, data=json.dumps({"content": f"**Ordo deploy** {text}"}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "ordo-deploy"})
        try:
            urllib.request.urlopen(request, timeout=20).close()
        except (urllib.error.URLError, OSError) as e:
            print(f"could not post to Discord: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# The lock and the schedule.
# --------------------------------------------------------------------------- #


def acquire_lock(path: Path, *, now: float | None = None) -> bool:
    """Create the lock file, or report that another run holds it. A lock older than
    STALE_LOCK_SECONDS is a dead run's and is taken over."""
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if now - path.stat().st_mtime > STALE_LOCK_SECONDS:
            path.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as stream:
        stream.write(str(os.getpid()))
    return True


def install_schedule(*, out: Path, project: str, repo: Path = REPO_ROOT) -> int:
    """Register the scheduled task that runs `ordo deploy` every SCHEDULE_MINUTES (Windows), or
    print the crontab line to add (elsewhere). Re-running replaces the task."""
    python = Path(sys.executable)
    args = f"-m ordo deploy --out {out} --project {project}"
    if os.name != "nt":
        print(f"add to the operator's crontab (crontab -e):\n"
              f"*/{SCHEDULE_MINUTES} * * * * cd {repo} && {python} {args} >> {out}/deploy/deploy.log 2>&1")
        return 0
    # pythonw has no console, so the task never flashes a window; S4U runs it whether or not the
    # operator is signed in, as the operator, with no stored password (the repo is fetched without
    # credentials, and docker answers the operator's account).
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python
    log = Path(out) / "deploy" / "deploy.log"
    script = f"""
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args} --log {log}' -WorkingDirectory '{repo}'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes {SCHEDULE_MINUTES})
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName '{SCHEDULED_TASK_NAME}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)
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
