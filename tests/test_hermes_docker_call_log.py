"""The `docker` wrapper that logs the agent's CLI calls (services/hermes/docker-call-log) and the
summary that reads the log (services/hermes/scripts/docker_call_summary.py).

The wrapper is temporary measurement for removing the agent's Docker socket. It must record each
call with secrets redacted, and change nothing about docker: the real CLI always runs with the
original arguments and its exit code is the caller's, even when the log cannot be written.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "services" / "hermes" / "docker-call-log"
SUMMARY = ROOT / "services" / "hermes" / "scripts" / "docker_call_summary.py"
REAL_CLI = "/usr/bin/docker"

needs_posix = pytest.mark.skipif(
    shutil.which("sh") is None or not hasattr(os, "getuid"),
    reason="needs a POSIX sh and os.getuid (the wrapper execs a real process)")


def wrapper_with_stub_cli(tmp_path: Path) -> Path:
    """A copy of the wrapper whose exec target is a stub that prints the arguments it received."""
    stub = tmp_path / "docker-stub"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "arg=$a"; done\nexit "${STUB_EXIT:-0}"\n', newline="\n")
    stub.chmod(0o755)
    source = WRAPPER.read_text(encoding="utf-8")
    assert source.count(REAL_CLI) == 1
    copy = tmp_path / "docker"
    copy.write_text(source.replace(REAL_CLI, str(stub)), newline="\n")
    return copy


def run(wrapper: Path, *args: str, log: Path | str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(wrapper), *args], capture_output=True, text=True,
                          env={**os.environ, "ORDO_DOCKER_CALL_LOG": str(log), **env})


def logged(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def logged_argv(tmp_path: Path, *args: str) -> list[str]:
    log = tmp_path / "calls.jsonl"
    run(wrapper_with_stub_cli(tmp_path), *args, log=log)
    return logged(log)[-1]["argv"]


@needs_posix
def test_logs_the_call_and_runs_the_cli_with_the_exact_arguments_and_exit_code(tmp_path):
    log = tmp_path / "logs" / "calls.jsonl"
    result = run(wrapper_with_stub_cli(tmp_path), "ps", "-a", "--format", "{{.Names}}", log=log, STUB_EXIT="3")
    assert result.returncode == 3
    assert result.stdout.splitlines() == ["arg=ps", "arg=-a", "arg=--format", "arg={{.Names}}"]
    (record,) = logged(log)
    assert record["argv"] == ["ps", "-a", "--format", "{{.Names}}"]
    assert record["cwd"] == os.getcwd()
    assert record["uid"] == os.getuid()
    assert record["ts"].endswith("Z") and len(record["ts"]) == len("2026-01-01T00:00:00Z")


@needs_posix
def test_every_call_appends_one_line(tmp_path):
    log = tmp_path / "calls.jsonl"
    wrapper = wrapper_with_stub_cli(tmp_path)
    run(wrapper, "ps", log=log)
    run(wrapper, "images", log=log)
    assert [record["argv"] for record in logged(log)] == [["ps"], ["images"]]


@needs_posix
def test_redacts_environment_values_in_both_forms(tmp_path):
    argv = logged_argv(tmp_path, "run", "-e", "TOKEN=abc", "--env", "KEY=def", "--env=OTHER=ghi",
                       "-e", "NOVALUE", "-e=FOO=bar", "img")
    assert argv == ["run", "-e", "TOKEN=<redacted>", "--env", "KEY=<redacted>", "--env=OTHER=<redacted>",
                    "-e", "NOVALUE", "-e=<redacted>", "img"]


@needs_posix
def test_redacts_the_attached_short_environment_form(tmp_path):
    assert logged_argv(tmp_path, "run", "-eTOKEN=secret", "image") == ["run", "-eTOKEN=<redacted>", "image"]


@needs_posix
def test_redacts_bundled_short_flags_and_the_attached_bundled_environment_form(tmp_path):
    # pflag bundles short flags: `-ite` is `-i -t -e`, so the next argument is the env value, and
    # `e` may glue its own value onto the same token. The control `-itx` does not end in `e`.
    assert logged_argv(tmp_path, "run", "-ite", "TOKEN=abc123", "img") == ["run", "-ite", "TOKEN=<redacted>", "img"]
    assert logged_argv(tmp_path, "run", "-iteSECRET=abc123", "img") == ["run", "-iteSECRET=<redacted>", "img"]
    assert logged_argv(tmp_path, "run", "-itx", "TOKEN=abc123", "img") == ["run", "-itx", "TOKEN=abc123", "img"]
    # A bundle ending in `e` always consumes the next argument as its value, with or without `=`.
    assert logged_argv(tmp_path, "run", "-ite", "NOVALUE", "img") == ["run", "-ite", "NOVALUE", "img"]


@needs_posix
def test_the_log_file_is_writable_by_every_user_of_the_container(tmp_path):
    logged_argv(tmp_path, "ps")
    assert (tmp_path / "calls.jsonl").stat().st_mode & 0o777 == 0o666


@needs_posix
def test_a_planted_symlink_at_the_log_path_is_skipped(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("untouched\n", encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "calls.jsonl"
    os.symlink(target, link)
    result = run(wrapper_with_stub_cli(tmp_path), "ps", log=link)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["arg=ps"]
    assert os.path.islink(link)
    assert target.read_text(encoding="utf-8") == "untouched\n"
    assert target.stat().st_mode & 0o777 == 0o644


@needs_posix
def test_redacts_build_arg_values_in_both_forms(tmp_path):
    argv = logged_argv(tmp_path, "build", "--build-arg", "A=1", "--build-arg=B=2", ".")
    assert argv == ["build", "--build-arg", "A=<redacted>", "--build-arg=B=<redacted>", "."]


@needs_posix
def test_redacts_the_login_password_but_not_a_run_port(tmp_path):
    assert logged_argv(tmp_path, "login", "-u", "me", "-p", "hunter2", "registry") == [
        "login", "-u", "me", "-p", "<redacted>", "registry"]
    assert logged_argv(tmp_path, "login", "--password", "hunter2") == ["login", "--password", "<redacted>"]
    assert logged_argv(tmp_path, "login", "--password=hunter2") == ["login", "--password=<redacted>"]
    assert logged_argv(tmp_path, "run", "-p", "8080:80", "img") == ["run", "-p", "8080:80", "img"]


@needs_posix
def test_redacts_the_attached_short_form_password_after_login_only(tmp_path):
    # A glued `-p` value carries the whole password; elsewhere (`run`) the glued `-p` is a port spec.
    assert logged_argv(tmp_path, "login", "-u", "me", "-phunter2", "registry") == [
        "login", "-u", "me", "<redacted>", "registry"]
    assert logged_argv(tmp_path, "login", "-p=pw12", "registry") == ["login", "<redacted>", "registry"]
    assert logged_argv(tmp_path, "run", "-p8080:80", "img") == ["run", "-p8080:80", "img"]


@needs_posix
def test_rotation_removes_a_planted_symlink_and_recovers_logging(tmp_path):
    # An oversized symlinked log is rotated by renaming the link itself, so the next call writes anew.
    target = tmp_path / "huge.jsonl"
    with target.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)
    link = tmp_path / "calls.jsonl"
    os.symlink(target, link)
    run(wrapper_with_stub_cli(tmp_path), "ps", log=link)
    assert not os.path.islink(link)
    assert os.path.islink(tmp_path / "calls.jsonl.1")
    assert os.path.getsize(target) == 20 * 1024 * 1024 + 1  # the target itself stayed put
    assert [record["argv"] for record in logged(link)] == [["ps"]]


@needs_posix
def test_the_real_cli_gets_the_unredacted_arguments(tmp_path):
    result = run(wrapper_with_stub_cli(tmp_path), "run", "-e", "TOKEN=abc", "img", log=tmp_path / "calls.jsonl")
    assert "arg=TOKEN=abc" in result.stdout.splitlines()


@needs_posix
def test_truncates_a_long_argument(tmp_path):
    argv = logged_argv(tmp_path, "exec", "agent", "x" * 501, "y" * 500)
    assert argv == ["exec", "agent", "x" * 500 + "...", "y" * 500]


@needs_posix
def test_still_runs_the_cli_when_the_log_cannot_be_written(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    result = run(wrapper_with_stub_cli(tmp_path), "ps", log=blocker / "sub" / "calls.jsonl", STUB_EXIT="5")
    assert result.returncode == 5
    assert result.stdout.splitlines() == ["arg=ps"]
    assert result.stderr == ""


@needs_posix
def test_rotates_a_log_over_the_size_limit_and_replaces_the_older_generation(tmp_path):
    log = tmp_path / "calls.jsonl"
    older = tmp_path / "calls.jsonl.1"
    older.write_text("very old\n")
    with log.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024 + 1)  # sparse: no 20 MB is written
    run(wrapper_with_stub_cli(tmp_path), "ps", log=log)
    assert older.stat().st_size == 20 * 1024 * 1024 + 1
    assert [record["argv"] for record in logged(log)] == [["ps"]]


@needs_posix
def test_a_log_at_the_limit_is_not_rotated(tmp_path):
    log = tmp_path / "calls.jsonl"
    with log.open("wb") as handle:
        handle.truncate(20 * 1024 * 1024)
    run(wrapper_with_stub_cli(tmp_path), "ps", log=log)
    assert not (tmp_path / "calls.jsonl.1").exists()


def test_the_wrapper_keeps_the_real_cli_path_once_and_has_a_posix_shebang():
    source = WRAPPER.read_bytes()
    assert source.startswith(b"#!/usr/bin/python3\n")
    assert b"\r" not in source
    assert source.decode().count(REAL_CLI) == 1


def load_summary():
    spec = importlib.util.spec_from_file_location("docker_call_summary", SUMMARY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


summary = load_summary()


@pytest.mark.parametrize("argv, verb", [
    (["ps", "-a"], "ps"),
    (["compose", "up", "-d"], "compose up"),
    (["compose", "-f", "x.yml", "--profile", "p", "up"], "compose up"),
    (["-H", "tcp://example.invalid:2375", "ps"], "ps"),
    (["--host=tcp://example.invalid:2375", "--context", "other", "-c", "cfg", "-l", "debug", "images"], "images"),
    (["container", "ls", "-a"], "container ls"),
    (["image", "prune"], "image prune"),
    (["volume", "ls"], "volume ls"),
    (["network", "inspect", "net"], "network inspect"),
    (["buildx", "build", "."], "buildx build"),
    (["system", "df"], "system df"),
    ([], ""),
])
def test_split_verb(argv, verb):
    assert summary.split_verb(argv)[0] == verb


def test_image_names_for_run_create_pull_and_build():
    def images(argv):
        verb, rest = summary.split_verb(argv)
        return summary.image_names(verb, rest)

    assert images(["run", "--rm", "-e", "A=1", "-p", "80:80", "--name", "n", "alpine:3", "sh"]) == ["alpine:3"]
    assert images(["create", "-v", "/a:/b", "busybox"]) == ["busybox"]
    assert images(["pull", "ubuntu:24.04"]) == ["ubuntu:24.04"]
    assert images(["build", "-t", "one:1", "--tag=two:2", "."]) == ["one:1", "two:2"]
    assert images(["ps"]) == []


def test_exec_target_is_the_container_and_the_first_word_of_the_command():
    def target(argv):
        verb, rest = summary.split_verb(argv)
        assert verb == "exec"
        return summary.exec_target(rest)

    assert target(["exec", "-it", "-u", "root", "agent", "python3", "-c", "print(1)"]) == "agent python3"
    assert target(["exec", "agent", "sh -c 'ls'"]) == "agent sh"
    assert target(["exec", "agent"]) == "agent"
    assert target(["exec"]) is None


def test_target_containers():
    def targets(argv):
        verb, rest = summary.split_verb(argv)
        return summary.target_containers(verb, rest)

    assert targets(["logs", "--tail", "50", "-f", "web"]) == ["web"]
    assert targets(["inspect", "-f", "{{.State}}", "a", "b"]) == ["a", "b"]
    assert targets(["restart", "-t", "5", "web"]) == ["web"]
    assert targets(["rm", "-f", "old"]) == ["old"]
    assert targets(["cp", "web:/etc/hosts", "./hosts"]) == ["web"]


def test_bad_lines_are_counted_and_skipped():
    lines = ['{"ts": "2026-01-01T00:00:00Z", "argv": ["ps"]}', "not json", "", "[1]", '{"argv": "ps"}']
    records, unreadable = summary.parse_log_lines(lines)
    assert [record["argv"] for record in records] == [["ps"]]
    assert unreadable == 3


def test_summarize_counts_everything():
    records = [
        {"ts": "2026-01-02T00:00:00Z", "argv": ["compose", "up", "-d"]},
        {"ts": "2026-01-01T00:00:00Z", "argv": ["run", "--rm", "alpine", "true"]},
        {"ts": "2026-01-03T00:00:00Z", "argv": ["exec", "agent", "python3", "x.py"]},
        {"ts": "2026-01-03T00:00:00Z", "argv": ["exec", "agent", "python3"]},
        {"ts": "2026-01-03T00:00:00Z", "argv": ["logs", "web"]},
    ]
    result = summary.summarize(records, unreadable_lines=2)
    assert result["total_calls"] == 5
    assert (result["first_call"], result["last_call"]) == ("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    assert result["unreadable_lines"] == 2
    assert result["verbs"] == {"exec": 2, "compose up": 1, "run": 1, "logs": 1}
    assert result["images"] == {"alpine": 1}
    assert result["exec_targets"] == {"agent python3": 2}
    assert result["container_targets"] == {"logs": {"web": 1}}


def test_the_script_reads_both_generations_and_prints_text_or_json(tmp_path):
    log = tmp_path / "calls.jsonl"
    log.write_text('{"ts": "2026-01-02T00:00:00Z", "argv": ["ps"]}\nbroken\n', encoding="utf-8")
    (tmp_path / "calls.jsonl.1").write_text('{"ts": "2026-01-01T00:00:00Z", "argv": ["images"]}\n', encoding="utf-8")
    text = subprocess.run([sys.executable, str(SUMMARY), str(log)], capture_output=True, text=True, check=True).stdout
    assert "docker calls: 2" in text
    assert "unreadable lines skipped: 1" in text
    as_json = subprocess.run([sys.executable, str(SUMMARY), str(log), "--json"], capture_output=True, text=True,
                             check=True).stdout
    assert json.loads(as_json)["verbs"] == {"images": 1, "ps": 1}


def test_a_missing_log_is_an_empty_summary(tmp_path):
    out = subprocess.run([sys.executable, str(SUMMARY), str(tmp_path / "absent"), "--json"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["total_calls"] == 0


def test_the_image_installs_the_wrapper_and_the_summary():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    lines = dockerfile.splitlines()
    assert "COPY docker-call-log /usr/local/bin/docker" in lines
    assert "COPY scripts/docker_call_summary.py /opt/ordo/docker_call_summary.py" in lines
    assert "/usr/local/bin/docker \\" in dockerfile  # the wrapper is made executable with the others
