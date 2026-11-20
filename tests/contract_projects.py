"""The throwaway compose projects the backend contract suite starts on a real docker engine, and
the removal of the ones a killed run leaves behind (tests/substrate/test_backend_contract.py).

A killed run (a timeout, a closed terminal, `kill`) runs no teardown, and its project stays on the
engine. On a host that also runs the stack that is not only clutter: the dashboard lists every
container on the host, so a leaked project shows up in the Services table and, through its
Homepage labels, in the Overview's launcher. tests/conftest.py removes stale projects at the start
of every pytest session, so a leak lasts until the next run of any test, not the next run of the
docker half of one suite.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                              capture_output=True, text=True, timeout=30)
        compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return info.returncode == 0 and compose.returncode == 0


# Every real project this suite starts is named with this prefix, and nothing else on a host is.
PROJECT_PREFIX = "ordo-contract-"
# A contract project is a leak only once its newest container is older than this. Several runs of
# this suite share one engine (parallel agents, a developer beside CI), so "started before my
# session" is not a leak: a concurrent run started seconds earlier. Two hours is far above the
# suite's runtime (minutes) and the CI docker job's 30-minute timeout, so no live run is that old.
STALE_PROJECT_AGE = timedelta(hours=2)


def parse_created(value: str) -> datetime:
    """docker's `.Created` (RFC 3339 with nanoseconds, e.g. 2026-09-29T16:39:58.123456789Z) as an
    aware datetime. fromisoformat takes at most microseconds, so the fraction is cut to 6 digits."""
    stamp = value.strip().replace("Z", "+00:00")
    head, dot, rest = stamp.partition(".")
    if dot:
        fraction_length = len(rest) - len(rest.lstrip("0123456789"))
        stamp = f"{head}.{rest[:min(fraction_length, 6)]}{rest[fraction_length:]}"
    return datetime.fromisoformat(stamp)


def stale_projects(containers: list[tuple[str, str]], now: datetime,
                   max_age: timedelta = STALE_PROJECT_AGE) -> list[str]:
    """The contract projects whose newest container is older than `max_age` at `now`: left behind
    by a run that was interrupted or killed before its teardown ran. Any younger project may belong
    to a run in progress, this one or a concurrent one, and is kept. `containers` is (project,
    created)."""
    newest: dict[str, datetime] = {}
    for project, created in containers:
        if not project.startswith(PROJECT_PREFIX):
            continue
        when = parse_created(created)
        if project not in newest or when > newest[project]:
            newest[project] = when
    return sorted(project for project, when in newest.items() if now - when > max_age)


def contract_containers() -> list[tuple[str, str]]:
    """(compose project, created) for every container of a contract project on this engine."""
    listed = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{PROJECT_PREFIX}"],
                            capture_output=True, text=True, timeout=30, check=True)
    ids = listed.stdout.split()
    if not ids:
        return []
    template = '{{index .Config.Labels "com.docker.compose.project"}}|{{.Created}}'
    inspected = subprocess.run(["docker", "inspect", "--type", "container", "--format", template, *ids],
                               capture_output=True, text=True, timeout=30, check=True)
    rows = [line.split("|", 1) for line in inspected.stdout.splitlines() if "|" in line]
    return [(project, created) for project, created in rows if project]


def remove_project(project: str) -> None:
    """Take a contract project down by its compose labels (no compose file needed)."""
    subprocess.run(["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans", "--timeout", "1"],
                   capture_output=True, text=True, timeout=180)


def remove_stale_projects(max_age: timedelta = STALE_PROJECT_AGE) -> list[str]:
    """Remove every stale contract project on this engine and return their names. Without a docker
    daemon, or with one that does not answer, there is nothing to do and nothing is raised: this
    runs before test sessions that never touch docker."""
    if shutil.which("docker") is None:
        return []
    try:
        stale = stale_projects(contract_containers(), datetime.now(UTC), max_age)
        for project in stale:
            remove_project(project)
    except (OSError, subprocess.SubprocessError):
        return []
    return stale
