"""The CPU fallback's CPU budget is a declared, rendered setting.

While a render holds the GPU, chat fails over to `llamacpp-cpu`. It used to run with no `--threads`
and no CPU limit, so llama.cpp took about half of a 48-thread host and pushed the operator's UPS into
its overload alarm. The budget is now `overrides.llamacpp-cpu.threads`: it defaults to
min(12, half the detected logical CPUs) and renders to `--threads`, `--threads-batch` and the
service's compose `cpus` limit, so the argv and the ceiling cannot disagree.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ordo.host import bringup, doctor
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


def _hardware(cpu_cores: int) -> dict:
    return {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0", "uuid": "GPU-aaaa"}],
            "ram_gb": 128, "cpu_cores": cpu_cores, "platform": "Linux"}


def _render(cpu_cores: int = 48, overrides: dict | None = None):
    return render(Source.from_dict({"hardware": _hardware(cpu_cores), "plugins": ["llamacpp-cpu"],
                                    "overrides": overrides or {}}), CATALOG, REGISTRY)


def _interpolate(value: str, env: dict[str, str]) -> str:
    """Resolve a `${VAR?message}` compose value against the rendered .env, failing as compose would."""
    if not (value.startswith("${") and value.endswith("}")):
        return value
    name, _, message = value[2:-1].partition("?")
    assert name in env, f"compose would refuse: {message}"
    return env[name]


def _cpu_fallback_service(rc) -> dict:
    return rc.compose_dict()["services"]["llamacpp-cpu"]


def _flag(command: list[str], flag: str) -> str:
    assert command.count(flag) == 1, f"{flag} must appear exactly once in {command}"
    return str(command[command.index(flag) + 1])


@pytest.mark.parametrize("cpu_cores,expected", [(48, 12), (32, 12), (24, 12), (16, 8), (8, 4), (2, 1), (1, 1)])
def test_default_is_half_the_detected_cpus_capped_at_12(cpu_cores, expected):
    rc = _render(cpu_cores)
    assert rc.env["LLAMACPP_CPU_THREADS"] == str(expected)


def test_threads_render_to_the_argv_and_the_cpu_limit():
    rc = _render(48)
    service = _cpu_fallback_service(rc)
    command = [str(c) for c in service["command"]]
    threads = _interpolate(_flag(command, "--threads"), rc.env)
    threads_batch = _interpolate(_flag(command, "--threads-batch"), rc.env)
    cpus = _interpolate(service["deploy"]["resources"]["limits"]["cpus"], rc.env)
    assert threads == threads_batch == cpus == "12"


def test_manifest_carries_no_fallback_that_could_disagree_with_the_render():
    service = _cpu_fallback_service(_render(48))
    command = [str(c) for c in service["command"]]
    for value in (_flag(command, "--threads"), _flag(command, "--threads-batch"),
                  service["deploy"]["resources"]["limits"]["cpus"]):
        assert value.startswith("${LLAMACPP_CPU_THREADS?"), value


def test_override_sets_the_threads():
    rc = _render(48, {"llamacpp-cpu": {"threads": 6}})
    assert rc.env["LLAMACPP_CPU_THREADS"] == "6"


def test_override_may_use_every_detected_cpu():
    assert _render(16, {"llamacpp-cpu": {"threads": 16}}).env["LLAMACPP_CPU_THREADS"] == "16"


@pytest.mark.parametrize("threads", [0, -2, 49, "12", 4.5, True, None])
def test_threads_outside_one_to_the_detected_cpus_is_a_render_error(threads):
    with pytest.raises(ValueError) as err:
        _render(48, {"llamacpp-cpu": {"threads": threads}})
    message = str(err.value)
    assert "llamacpp-cpu" in message and "threads" in message and "48" in message


def test_unknown_llamacpp_cpu_override_key_is_a_render_error_naming_threads():
    with pytest.raises(ValueError) as err:
        _render(48, {"llamacpp-cpu": {"cpus": 4}})
    message = str(err.value)
    assert "'cpus'" in message and "threads" in message


def test_the_gpu_chat_service_is_untouched():
    """GPU token throughput must not move: the budget applies to the CPU fallback only."""
    default = _render(48).compose_dict()["services"]["llamacpp"]
    capped = _render(48, {"llamacpp-cpu": {"threads": 4}}).compose_dict()["services"]["llamacpp"]
    assert capped == default
    assert "LLAMACPP_CPU_THREADS" not in str(default)
    assert "limits" not in default.get("deploy", {}).get("resources", {})


# --- ordo doctor shows the declared budget next to what the running container has ---

def _inspect_output(cmd: list[str], nano_cpus: int) -> str:
    return json.dumps([{"Config": {"Cmd": cmd}, "HostConfig": {"NanoCpus": nano_cpus}}])


def _fake_docker(monkeypatch, *, cmd: list[str] | None, nano_cpus: int = 0, returncode: int = 0):
    monkeypatch.setattr(bringup, "find_running_container",
                        lambda project, service: "ordo-llamacpp-cpu-1" if cmd is not None else None)

    def run(argv, **kwargs):
        assert argv[:2] == ["docker", "inspect"] and argv[-1] == "ordo-llamacpp-cpu-1"
        return subprocess.CompletedProcess(argv, returncode, _inspect_output(cmd or [], nano_cpus), "boom")

    monkeypatch.setattr(doctor.subprocess, "run", run)


def test_doctor_flags_a_running_container_without_the_declared_threads(monkeypatch):
    """The state a `docker update --cpus 12` leaves: the ceiling is right, llama.cpp still has no
    --threads, and nothing in the config hash says so. Doctor names it until the recreate."""
    _fake_docker(monkeypatch, cmd=["--host", "0.0.0.0", "--n-gpu-layers", "0"], nano_cpus=12 * 10**9)
    ok, line = doctor.cpu_fallback_check("ordo", 12)
    assert not ok
    assert "declared threads=12" in line and "threads=unset" in line and "cpus=12" in line
    assert "ordo apply --only llamacpp-cpu" in line


def test_doctor_flags_a_running_container_without_a_cpu_limit(monkeypatch):
    _fake_docker(monkeypatch, cmd=["--threads", "12", "--threads-batch", "12"], nano_cpus=0)
    ok, line = doctor.cpu_fallback_check("ordo", 12)
    assert not ok and "cpus=unlimited" in line


def test_doctor_passes_when_the_running_container_matches(monkeypatch):
    _fake_docker(monkeypatch, cmd=["--threads", "12", "--threads-batch", "12"], nano_cpus=12 * 10**9)
    ok, line = doctor.cpu_fallback_check("ordo", 12)
    assert ok and line == "cpu-fallback: threads=12, cpus=12 (declared and running)"


def test_doctor_reports_the_declared_value_when_the_fallback_is_not_running(monkeypatch):
    _fake_docker(monkeypatch, cmd=None)
    ok, line = doctor.cpu_fallback_check("ordo", 12)
    assert ok and "threads=12" in line and "not running" in line


def test_doctor_is_silent_about_a_fallback_the_render_does_not_define(monkeypatch):
    ok, line = doctor.cpu_fallback_check("ordo", None)
    assert ok and "not rendered" in line


def test_doctor_fails_when_the_container_cannot_be_inspected(monkeypatch):
    _fake_docker(monkeypatch, cmd=[], returncode=1)
    ok, line = doctor.cpu_fallback_check("ordo", 12)
    assert not ok and line.startswith("! cpu-fallback: cannot inspect")
