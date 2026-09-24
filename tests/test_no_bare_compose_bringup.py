"""No tracked recipe brings the stack up with a hand-assembled `docker compose ... up` or `restart`.

`ordo up` / `ordo recreate` are the only bring-up verbs: they share ops-controller's argv builder (both
env files, every profile, `--no-deps` for named services, caddy's netns members named alongside it) and
refuse while the GPU lease would be violated. A bare `docker compose up -d agent` starts the agent's
whole dependency closure, which contains `llamacpp`: during a leased render that puts the evicted GPU
resident back beside the render (the 2026-09-23 near-miss shape). A bare `docker compose restart`
starts a stopped (evicted) container the same way.

The one sanctioned compose call is the evals one-shot, `docker compose -p ordo --profile evals run
--rm evals`: a `run --rm`, not a bring-up.

Scope: every tracked Markdown file (a recipe an operator or an agent copies) and every tracked
script. CHANGELOG.md is history and is exempt.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPT_SUFFIXES = (".sh", ".ps1", ".cmd", ".bat")
EXEMPT_FILES = {"CHANGELOG.md"}

# Lines that name the forbidden form in order to forbid it: (path, a phrase on that line).
PROHIBITIONS = {
    ("AGENTS.md", "Never hand-assemble"),
}

COMPOSE_CALL = re.compile(r"docker[ -]compose\b")
BRINGUP_VERB = re.compile(r"(?<![\w-])(up|restart)(?![\w-])")


def _tracked_recipe_files() -> list[str]:
    listed = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files"], capture_output=True, text=True,
                            check=True).stdout.splitlines()
    return sorted(path for path in listed
                  if path not in EXEMPT_FILES
                  and (path.endswith(".md") or path.endswith(SCRIPT_SUFFIXES) or path.startswith("scripts/")))


def bare_bringups(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every compose call on a line whose own command runs `up` or `restart`.

    The command is the text after `docker compose` up to the closing backtick of its code span (or
    the end of the line), so a later `ordo up` in the same sentence is not mistaken for part of it."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in COMPOSE_CALL.finditer(line):
            command = line[match.end():].split("`", 1)[0]
            if "run --rm" in command:
                continue
            if BRINGUP_VERB.search(command):
                found.append((number, line.strip()))
                break
    return found


def test_the_detector_flags_bringups_and_spares_the_evals_one_shot():
    assert bare_bringups("`docker compose -p ordo --env-file .env up -d agent`")
    assert bare_bringups("docker compose restart oauth2-proxy")
    assert bare_bringups("re-run `docker compose -p ordo … up` from `out/`")
    assert not bare_bringups("`docker compose -p ordo --profile evals run --rm evals`")
    assert not bare_bringups("`docker compose build` there does nothing, so run `ordo up`")
    assert not bare_bringups("docker compose -f x.yml --env-file .env config >/dev/null")


def test_no_tracked_recipe_runs_a_bare_compose_bringup():
    offenders = []
    for path in _tracked_recipe_files():
        file = REPO_ROOT / path
        if not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        for number, line in bare_bringups(text):
            if any(path == allowed and phrase in line for allowed, phrase in PROHIBITIONS):
                continue
            offenders.append(f"{path}:{number}: {line}")
    assert not offenders, ("use `ordo up <svc>` / `ordo recreate <svc>` (lease-checked, --no-deps) instead "
                           "of a bare compose bring-up:\n" + "\n".join(offenders))
