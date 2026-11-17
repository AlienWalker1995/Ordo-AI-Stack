#!/usr/bin/python3
"""Summarise the agent's docker CLI calls from the log written by services/hermes/docker-call-log.

Usage: docker_call_summary.py [LOG_PATH] [--json]
Reads LOG_PATH (default /opt/data/logs/docker-calls.jsonl) and every rotated generation the wrapper
keeps (.1 is the newest rotation, .N the oldest; keep in sync with the wrapper's MAX_GENERATIONS).
Option parsing is a heuristic: an option it does not know is taken to be a flag without a value.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

DEFAULT_LOG_PATH = "/opt/data/logs/docker-calls.jsonl"
# The wrapper's rotation keeps this many full generations behind the live log; reading them all is
# what carries the measurement across the seven-day window card 2 needs. Keep in sync with the
# wrapper's MAX_GENERATIONS.
ROTATED_GENERATIONS = 7

# Global options that take a separate value, skipped when looking for the verb.
GLOBAL_VALUE_OPTIONS = {"-H", "--host", "--context", "-c", "--config", "-l", "--log-level"}
# Subcommand groups: the verb is the group plus its first sub-verb, for example `compose up`.
GROUP_VERBS = {"compose", "container", "image", "volume", "network", "buildx", "system"}
COMPOSE_VALUE_OPTIONS = {"-f", "--file", "-p", "--project-name", "--profile", "--env-file",
                         "--project-directory", "--ansi", "--parallel", "--progress"}
# `run` and `create` options that take a separate value, skipped when looking for the image. The
# list must cover every value-taking flag: an unknown option is assumed to take no value, so a
# missed entry leaks the option's value into the reported image.
RUN_VALUE_OPTIONS = {
    "-a", "--attach", "-e", "--env", "-p", "--publish", "-v", "--volume", "--name", "--network",
    "--net", "-w", "--workdir", "-u", "--user", "--entrypoint", "-m", "--memory", "--cpus", "-l",
    "--label", "--restart", "--mount", "--platform", "--pull", "--add-host", "--cap-add", "--cap-drop",
    "--device", "--env-file", "-h", "--hostname", "--ipc", "--pid", "--gpus", "--runtime",
    "--security-opt", "--shm-size", "--tmpfs", "--group-add", "--log-driver", "--log-opt", "--dns",
    "--label-file", "--cidfile", "--pids-limit", "--detach-keys", "--ulimit", "--volumes-from",
    "--expose", "--ip", "--ip6", "--link", "--stop-signal", "--stop-timeout", "--sysctl",
    "--storage-opt", "--cpuset-cpus", "--cpuset-mems", "--cpu-period", "--cpu-quota", "--cpu-shares",
    "--cpu-rt-period", "--cpu-rt-runtime", "--blkio-weight", "--memory-swap", "--memory-reservation",
    "--memory-swappiness", "--oom-score-adj", "--kernel-memory", "--health-cmd", "--health-interval",
    "--health-timeout", "--health-start-period", "--health-start-interval", "--health-retries",
    "--mac-address", "--dns-option", "--dns-search", "--no-proxy", "--network-alias",
    "--device_cgroup_rules", "--cgroup-parent", "--cgroupns", "--userns", "--uts", "--isolation",
    "--sdnotify", "--secret", "--input-mode",
}
EXEC_VALUE_OPTIONS = {"-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys"}
# `compose run` takes the project options and run-style overrides around the service name; the
# first positional is the service, which names the workload the same way an image name does.
COMPOSE_RUN_VALUE_OPTIONS = RUN_VALUE_OPTIONS | COMPOSE_VALUE_OPTIONS | {"--scale", "--exit-code-from"}
# `compose exec` adds --index to the plain exec flags.
COMPOSE_EXEC_VALUE_OPTIONS = EXEC_VALUE_OPTIONS | {"--index"}
# pflag bundles short flags. When the bundle's last letter is one whose short flag takes a value
# (-a attach, -c console, -e env, -h hostname, -l label, -m memory, -n network, -p publish,
# -u user, -v volume, -w workdir), the next argument is that value, exactly as the wrapper's
# redaction assumes for the `e` case. `-t` is deliberately out: it is a boolean for `run`/`exec`
# (tty) but value-taking for `stop`/`restart`, so a uniform rule must not consume after it.
SHORT_FLAG_VALUE_LETTERS = "acehlmnpuvw"
BUNDLE_VALUE_INTRODUCER_RE = re.compile(rf"^-[A-Za-z]*[{SHORT_FLAG_VALUE_LETTERS}]$")
# Per verb, the options that take a value, for the verbs whose target containers are listed.
TARGET_VALUE_OPTIONS = {
    "logs": {"-n", "--tail", "--since", "--until"},
    "inspect": {"-f", "--format", "--type"},
    "restart": {"-t", "--time", "-s", "--signal"},
    "stop": {"-t", "--time"},
    "kill": {"-s", "--signal"},
    "start": set(),
    "rm": set(),
    "cp": set(),
}


def takes_value(option: str, words_so_far: list[str]) -> bool:
    if not words_so_far:
        return option in GLOBAL_VALUE_OPTIONS
    if words_so_far[0] == "compose":
        return option in COMPOSE_VALUE_OPTIONS
    return False


def split_verb(arguments: list[str]) -> tuple[str, list[str]]:
    """The docker verb (two words for a group such as `compose up`) and the arguments after it."""
    words: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token.startswith("-"):
            index += 2 if takes_value(token, words) else 1
            continue
        words.append(token)
        index += 1
        if words[0] not in GROUP_VERBS or len(words) == 2:
            break
    return " ".join(words), arguments[index:]


def positional_arguments(arguments: list[str], value_options: set[str]) -> list[str]:
    """The arguments that are neither options nor option values."""
    positionals = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in value_options:
            index += 2
        elif token.startswith("-"):
            # A short-flag bundle ending in a value-taking letter (-ite, -itv, ...) consumes its value.
            index += 2 if BUNDLE_VALUE_INTRODUCER_RE.match(token) else 1
        else:
            positionals.append(token)
            index += 1
    return positionals


def build_tags(arguments: list[str]) -> list[str]:
    """The image names given to `build -t/--tag`."""
    tags = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in ("-t", "--tag") and index + 1 < len(arguments):
            tags.append(arguments[index + 1])
            index += 2
        elif token.startswith("--tag="):
            tags.append(token[len("--tag="):])
            index += 1
        else:
            index += 1
    return tags


def image_names(verb: str, rest: list[str]) -> list[str]:
    if verb in ("run", "create", "container run", "container create"):
        return positional_arguments(rest, RUN_VALUE_OPTIONS)[:1]
    if verb == "compose run":
        return positional_arguments(rest, COMPOSE_RUN_VALUE_OPTIONS)[:1]
    if verb in ("pull", "image pull"):
        return positional_arguments(rest, set())[:1]
    if verb in ("build", "image build", "buildx build"):
        return build_tags(rest)
    return []


def exec_target(rest: list[str], value_options: set[str] = EXEC_VALUE_OPTIONS) -> str | None:
    """The container and the first word of the command, for example `agent python3`."""
    positionals = positional_arguments(rest, value_options)
    if not positionals:
        return None
    command_words = positionals[1].split() if len(positionals) > 1 else []
    if not command_words:
        return positionals[0]
    return f"{positionals[0]} {command_words[0]}"


def target_containers(verb: str, rest: list[str]) -> list[str]:
    names = positional_arguments(rest, TARGET_VALUE_OPTIONS[verb])
    if verb == "cp":
        # Only `container:path` arguments name a container; the other side is a local path.
        return [name.split(":")[0] for name in names if ":" in name]
    return names


def parse_log_lines(lines: list[str]) -> tuple[list[dict], int]:
    """The usable records in the log lines, and how many lines were not one."""
    records = []
    unreadable = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(record, dict) and isinstance(record.get("argv"), list):
            records.append(record)
        else:
            unreadable += 1
    return records, unreadable


def summarize(records: list[dict], unreadable_lines: int) -> dict:
    verbs: Counter = Counter()
    images: Counter = Counter()
    exec_targets: Counter = Counter()
    targets: dict[str, Counter] = {verb: Counter() for verb in TARGET_VALUE_OPTIONS}
    for record in records:
        verb, rest = split_verb([str(argument) for argument in record["argv"]])
        verbs[verb or "(none)"] += 1
        images.update(image_names(verb, rest))
        if verb in ("exec", "container exec"):
            target = exec_target(rest)
        elif verb == "compose exec":
            target = exec_target(rest, COMPOSE_EXEC_VALUE_OPTIONS)
        else:
            target = None
        if target:
            exec_targets[target] += 1
        if verb in TARGET_VALUE_OPTIONS:
            targets[verb].update(target_containers(verb, rest))
    timestamps = sorted(str(record["ts"]) for record in records if record.get("ts"))
    return {
        "total_calls": len(records),
        "first_call": timestamps[0] if timestamps else None,
        "last_call": timestamps[-1] if timestamps else None,
        "unreadable_lines": unreadable_lines,
        "verbs": dict(verbs.most_common()),
        "images": dict(images.most_common()),
        "exec_targets": dict(exec_targets.most_common()),
        "container_targets": {verb: dict(counter.most_common()) for verb, counter in targets.items() if counter},
    }


def read_lines(log_path: str) -> list[str]:
    """The rotated generations, oldest first, then the current log; any may be missing."""
    lines: list[str] = []
    paths = [f"{log_path}.{number}" for number in range(ROTATED_GENERATIONS, 0, -1)]
    paths.append(log_path)
    for path in paths:
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines.extend(handle.read().splitlines())
    return lines


def format_counts(title: str, counts: dict) -> list[str]:
    if not counts:
        return []
    return ["", title] + [f"  {count:6d}  {name}" for name, count in counts.items()]


def format_text(summary: dict) -> str:
    lines = [
        f"docker calls: {summary['total_calls']}",
        f"range: {summary['first_call']} to {summary['last_call']}",
        f"unreadable lines skipped: {summary['unreadable_lines']}",
    ]
    lines += format_counts("verbs", summary["verbs"])
    lines += format_counts("images (run, create, pull, build; compose run services)", summary["images"])
    lines += format_counts("exec targets", summary["exec_targets"])
    for verb, counts in summary["container_targets"].items():
        lines += format_counts(f"{verb} targets", counts)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    paths = [argument for argument in argv if argument != "--json"]
    log_path = paths[0] if paths else DEFAULT_LOG_PATH
    records, unreadable_lines = parse_log_lines(read_lines(log_path))
    summary = summarize(records, unreadable_lines)
    print(json.dumps(summary, indent=2) if as_json else format_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
