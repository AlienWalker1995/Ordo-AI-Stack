"""`ordo up` / `ordo recreate`: the one sanctioned host bring-up command.

Every host entry point used to hand-assemble `docker compose -p ordo --env-file ...` with its own
profile set and `--no-deps` choice, and none of them checked the GPU lease. A whole-stack `up -d`
during a render restarted the evicted llama.cpp beside the render (two tenants on one card). These
tests pin the argv the command builds (shared with ops-controller's DockerBackend, so the two
cannot diverge) and the lease refusals. Nothing touches docker: the status reader and
subprocess.run are replaced.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ordo import cli
from ordo.control.broker import DockerBackend
from ordo.host import bringup, cli_stack
from ordo.render import stack

COMPOSE = {
    "services": {
        "llamacpp": {"image": "x"},
        "model-gateway": {"image": "x", "depends_on": {"llamacpp": {"condition": "service_started"}}},
        "open-webui": {"image": "x", "profiles": ["webui"], "depends_on": ["model-gateway", "qdrant"]},
        "qdrant": {"image": "x", "profiles": ["rag"]},
        "ops-controller": {"image": "x"},
        "agent": {"image": "x", "depends_on": ["model-gateway"]},
        "oauth2-proxy": {"image": "x", "profiles": ["edge"]},
        "caddy": {"image": "x", "profiles": ["edge"], "depends_on": ["oauth2-proxy"]},
        "tailnet-chat": {"image": "x", "profiles": ["edge"], "network_mode": "service:caddy"},
        "hermes-dashboard": {"image": "x", "profiles": ["hermes-ui"], "network_mode": "service:caddy"},
    }
}

IDLE = {"state": "idle", "leased": False, "running": [], "queued": [], "evicted_residents": {}}
LEASED = {
    "state": "busy",
    "leased": True,
    "running": [{"id": "gate-comfyui", "kind": "media"}],
    "queued": [],
    "evicted_residents": {"llamacpp": 27.5},
}
# An ops-controller image older than the `leased` field: only the raw lists.
LEASED_OLD_IMAGE = {k: v for k, v in LEASED.items() if k != "leased"}
# A controller that writes its lease state to disk, and one whose last write failed.
LEASED_PERSISTED = {**LEASED, "state_persisted": True}
LEASED_NOT_SAVED = {**LEASED, "state_persisted": False}


@pytest.fixture(autouse=True)
def host_ready(monkeypatch):
    """These tests pin the argv and the lease refusals; the host preflight `ordo up` runs first
    is tested in test_preflight_host.py."""
    monkeypatch.setattr(cli_stack, "_host_preflight", lambda *a, **k: True)


@pytest.fixture
def out_dir(tmp_path) -> Path:
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE), encoding="utf-8")
    return tmp_path


@pytest.fixture
def recorded(monkeypatch) -> list[list[str]]:
    """Captures every argv the command would run; nothing reaches docker."""
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("ordo.host.bringup.subprocess.run", fake_run)
    return calls


@pytest.fixture
def persisting_out_dir(tmp_path) -> Path:
    """A render whose ops-controller declares where it keeps the scheduler state."""
    compose = yaml.safe_load(yaml.safe_dump(COMPOSE))
    compose["services"]["ops-controller"]["environment"] = {"SCHEDULER_STATE_PATH": "/data/scheduler-state.json"}
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose), encoding="utf-8")
    return tmp_path


def _status(monkeypatch, gpu):
    """Replace the ops-controller status reader. `gpu` None = ops-controller not running."""
    def fake(project):
        if isinstance(gpu, Exception):
            raise gpu
        return gpu

    monkeypatch.setattr("ordo.host.bringup.read_gpu_status", fake)


def _profiles(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--profile"]


def _tail(cmd: list[str]) -> list[str]:
    """The compose subcommand and its arguments (everything after the last global flag)."""
    return cmd[cmd.index("up"):]


# --- the shared argv builder ---


def test_compose_argv_shape():
    cmd = bringup.compose_argv("/d", "ordo", "up", "-d", profiles=["a", "b"])
    assert cmd == [
        "docker", "compose", "-p", "ordo", "-f", "/d/docker-compose.yml",
        "--profile", "a", "--profile", "b",
        "--env-file", "/d/.env", "--env-file", "/d/secrets.env", "--env-file", "/d/secret-files.env",
        "up", "-d",
    ]


def test_docker_backend_uses_the_shared_builder(out_dir):
    backend = DockerBackend("ordo")
    backend.COMPOSE_DIR = str(out_dir)
    assert backend._compose("up", "-d", "x", all_profiles=True) == bringup.compose_argv(
        str(out_dir), "ordo", "up", "-d", "x", profiles=bringup.profiles_in(COMPOSE))
    assert backend._compose("pull", "x") == bringup.compose_argv(str(out_dir), "ordo", "pull", "x")


def test_profiles_are_every_profile_in_the_rendered_compose_sorted():
    assert bringup.profiles_in(COMPOSE) == ["edge", "hermes-ui", "rag", "webui"]


# --- argv the CLI builds ---


def test_named_up_is_no_deps_with_both_env_files_and_every_profile(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "open-webui", "--out", str(out_dir)]) == 0
    cmd = recorded[-1]
    assert cmd[:4] == ["docker", "compose", "-p", "ordo"]
    assert cmd[cmd.index("-f") + 1] == f"{out_dir.resolve().as_posix()}/docker-compose.yml"
    env_files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--env-file"]
    assert env_files == [f"{out_dir.resolve().as_posix()}/.env", f"{out_dir.resolve().as_posix()}/secrets.env",
                         f"{out_dir.resolve().as_posix()}/secret-files.env"]
    assert _profiles(cmd) == ["edge", "hermes-ui", "rag", "webui"]
    assert _tail(cmd) == ["up", "-d", "--no-deps", "open-webui"]


def test_recreate_adds_force_recreate(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "model-gateway", "agent", "--out", str(out_dir)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d", "--no-deps", "--force-recreate", "model-gateway", "agent"]


def test_all_is_a_whole_stack_up_with_every_profile(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "--all", "--out", str(out_dir)]) == 0
    cmd = recorded[-1]
    assert _profiles(cmd) == ["edge", "hermes-ui", "rag", "webui"]
    assert _tail(cmd) == ["up", "-d"]


def test_core_is_a_whole_stack_up_with_no_profiles(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "--core", "--out", str(out_dir)]) == 0
    cmd = recorded[-1]
    assert _profiles(cmd) == []
    assert _tail(cmd) == ["up", "-d"]


def test_caddy_takes_its_netns_members_by_name_and_stays_no_deps(monkeypatch, out_dir, recorded):
    """The members are named, so compose recreates them after caddy; `--no-deps` keeps caddy's own
    dependencies (oauth2-proxy here, ops-controller and llamacpp live) out of the recreate."""
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "caddy", "--out", str(out_dir)]) == 0
    tail = _tail(recorded[-1])
    assert tail == ["up", "-d", "--no-deps", "--force-recreate", "caddy", "hermes-dashboard", "tailnet-chat"]


def test_a_netns_member_alone_is_still_no_deps(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "tailnet-chat", "--out", str(out_dir)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d", "--no-deps", "--force-recreate", "tailnet-chat"]


def test_dry_run_prints_the_argv_and_runs_nothing(monkeypatch, out_dir, recorded, capsys):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "--all", "--out", str(out_dir), "--dry-run"]) == 0
    assert recorded == []
    printed = capsys.readouterr().out
    assert "docker compose -p ordo" in printed and "secret-files.env up -d" in printed


def test_unknown_service_is_refused(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "no-such-thing", "--out", str(out_dir)]) == 1
    assert recorded == []


def test_up_needs_exactly_one_target_form(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "--out", str(out_dir)]) == 1
    assert cli.main(["up", "--all", "agent", "--out", str(out_dir)]) == 1
    assert cli.main(["up", "--all", "--core", "--out", str(out_dir)]) == 1
    assert recorded == []


def test_missing_render_is_refused(monkeypatch, tmp_path, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["up", "--all", "--out", str(tmp_path / "nope")]) == 1
    assert recorded == []


# --- the GPU lease ---


@pytest.mark.parametrize("gpu", [LEASED, LEASED_NOT_SAVED])
@pytest.mark.parametrize("argv", [["recreate", "ops-controller"], ["up", "ops-controller"],
                                  ["recreate", "ops-controller", "agent"]])
def test_ops_controller_is_refused_during_a_lease_without_persisted_state(
        monkeypatch, persisting_out_dir, recorded, capsys, gpu, argv):
    """Without persisted lease state a restart mid-lease loses it: the evicted resident is never
    restored, or is restored beside the render. An image older than persistence, or one whose last
    state write failed, does not report `state_persisted: true`."""
    _status(monkeypatch, gpu)
    assert cli.main([*argv, "--out", str(persisting_out_dir)]) == 2
    assert recorded == []
    err = capsys.readouterr().err
    assert "ops-controller" in err and "gate-comfyui" in err


def test_ops_controller_is_refused_during_a_lease_when_the_new_one_would_not_load_the_state(
        monkeypatch, out_dir, recorded, capsys):
    """The running controller saved its state, but the rendered replacement declares no state
    path, so it would start empty."""
    _status(monkeypatch, LEASED_PERSISTED)
    assert cli.main(["recreate", "ops-controller", "--out", str(out_dir)]) == 2
    assert recorded == []
    assert "SCHEDULER_STATE_PATH" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["recreate", "ops-controller"], ["up", "ops-controller"],
                                  ["recreate", "ops-controller", "agent"]])
def test_ops_controller_is_recreatable_during_a_lease_with_persisted_state(
        monkeypatch, persisting_out_dir, recorded, argv):
    """The running controller wrote its lease state to its data bind and the replacement loads
    it from the same path, so a recreate mid-lease keeps the resident evicted until the drain."""
    _status(monkeypatch, LEASED_PERSISTED)
    assert cli.main([*argv, "--out", str(persisting_out_dir)]) == 0
    assert "ops-controller" in _tail(recorded[-1])


def test_persisted_state_does_not_unlock_an_evicted_resident(monkeypatch, persisting_out_dir, recorded, capsys):
    _status(monkeypatch, LEASED_PERSISTED)
    assert cli.main(["recreate", "ops-controller", "llamacpp", "--out", str(persisting_out_dir)]) == 2
    assert recorded == []
    assert "llamacpp" in capsys.readouterr().err


def test_persisted_state_does_not_unlock_a_whole_stack_up(monkeypatch, persisting_out_dir, recorded):
    _status(monkeypatch, LEASED_PERSISTED)
    assert cli.main(["up", "--all", "--out", str(persisting_out_dir)]) == 2
    assert recorded == []


def test_ops_controller_is_recreatable_when_idle(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "ops-controller", "--out", str(out_dir)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d", "--no-deps", "--force-recreate", "ops-controller"]


def test_a_caddy_recreate_is_allowed_during_a_lease(monkeypatch, out_dir, recorded):
    """caddy has nothing to do with the card. Its dependency closure reaches ops-controller and
    the evicted llamacpp, but `--no-deps` starts only caddy and its named members, so neither the
    evicted resident nor the lease holder's control plane is touched."""
    compose = yaml.safe_load((out_dir / "docker-compose.yml").read_text(encoding="utf-8"))
    compose["services"]["oauth2-proxy"]["depends_on"] = ["llamacpp", "ops-controller"]
    (out_dir / "docker-compose.yml").write_text(yaml.safe_dump(compose), encoding="utf-8")
    _status(monkeypatch, LEASED)
    assert cli.main(["recreate", "caddy", "--out", str(out_dir)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d", "--no-deps", "--force-recreate",
                                   "caddy", "hermes-dashboard", "tailnet-chat"]


def test_no_ops_controller_running_proceeds(monkeypatch, out_dir, recorded):
    """A fresh install has no control plane yet, so there is no lease to honor."""
    _status(monkeypatch, None)
    assert cli.main(["up", "--all", "--out", str(out_dir)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d"]


def test_unreadable_status_fails_closed(monkeypatch, out_dir, recorded, capsys):
    _status(monkeypatch, bringup.LeaseUnknown("ops-controller is running but /status failed"))
    assert cli.main(["recreate", "model-gateway", "--out", str(out_dir)]) == 2
    assert recorded == []
    assert "/status failed" in capsys.readouterr().err


# --- the status reader ---


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_docker(monkeypatch, ps: _Proc, exec_: _Proc | None = None) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "ps"]:
            return ps
        if cmd[:2] == ["docker", "exec"] and exec_ is not None:
            return exec_
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("ordo.host.bringup.subprocess.run", fake_run)
    return calls


def test_status_reader_finds_the_container_by_compose_labels(monkeypatch):
    calls = _fake_docker(monkeypatch, _Proc(stdout="ordo-ops-controller-1\n"),
                         _Proc(stdout='{"state": "idle", "leased": false}\n'))
    assert bringup.read_gpu_status("ordo") == {"state": "idle", "leased": False}
    ps = calls[0]
    assert "label=com.docker.compose.project=ordo" in ps
    assert "label=com.docker.compose.service=ops-controller" in ps
    exec_ = calls[1]
    assert exec_[:3] == ["docker", "exec", "ordo-ops-controller-1"]
    script = exec_[-1]
    assert "http://127.0.0.1:9000/status" in script and "OPS_CONTROLLER_TOKEN" in script


def test_status_reader_returns_none_when_ops_controller_is_not_running(monkeypatch):
    _fake_docker(monkeypatch, _Proc(stdout=""))
    assert bringup.read_gpu_status("ordo") is None


@pytest.mark.parametrize("exec_", [
    _Proc(returncode=1, stderr="HTTP Error 401: Unauthorized"),
    _Proc(stdout="not json"),
    _Proc(stdout="[]"),
])
def test_status_reader_fails_closed_when_the_status_cannot_be_read(monkeypatch, exec_):
    _fake_docker(monkeypatch, _Proc(stdout="ordo-ops-controller-1\n"), exec_)
    with pytest.raises(bringup.LeaseUnknown):
        bringup.read_gpu_status("ordo")


def test_status_reader_fails_closed_when_docker_cannot_be_queried(monkeypatch):
    _fake_docker(monkeypatch, _Proc(returncode=1, stderr="Cannot connect to the Docker daemon"))
    with pytest.raises(bringup.LeaseUnknown):
        bringup.read_gpu_status("ordo")


# --- recreating the readers of rotated secrets ---

READERS_COMPOSE = {
    "services": {
        "ops-controller": {"image": "x", "environment": {"OPS_CONTROLLER_TOKEN": "${OPS_CONTROLLER_TOKEN}"}},
        "mcp-orchestration": {"image": "x", "environment": {"OPS_CONTROLLER_TOKEN": "${OPS_CONTROLLER_TOKEN}"}},
        # a secret mapped onto another name, with a fail-open default
        "agent": {"image": "x", "environment": {"HERMES_LANGFUSE_PUBLIC_KEY": "${LANGFUSE_PUBLIC_KEY:-}"}},
        # a secret read on the command line, not through the environment
        "langfuse-redis": {"image": "x", "command": ["--requirepass", "${LANGFUSE_REDIS_AUTH?missing}"]},
        # a one-shot job reads its environment per run; recreating it would start a run
        "evals": {"image": "x", "restart": "no", "environment": {"OPS_CONTROLLER_TOKEN": "${OPS_CONTROLLER_TOKEN}"}},
        # a longer name that merely starts with a rotated key is not that key
        "dashboard": {"image": "x", "environment": {"X": "${OPS_CONTROLLER_TOKEN_FILE:-}"}},
        "caddy": {"image": "x"},
    }
}


def test_readers_are_every_long_running_service_that_interpolates_a_key():
    assert stack.readers_of(READERS_COMPOSE, ["OPS_CONTROLLER_TOKEN"]) == ["mcp-orchestration", "ops-controller"]
    assert stack.readers_of(READERS_COMPOSE, ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_REDIS_AUTH"]) == [
        "agent", "langfuse-redis"]
    assert stack.readers_of(READERS_COMPOSE, ["NOT_READ_BY_ANYONE"]) == []


def test_recreate_reading_recreates_exactly_the_readers(monkeypatch, tmp_path, recorded):
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(READERS_COMPOSE), encoding="utf-8")
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "--reading", "OPS_CONTROLLER_TOKEN", "LANGFUSE_REDIS_AUTH",
                     "--out", str(tmp_path)]) == 0
    assert _tail(recorded[-1]) == ["up", "-d", "--no-deps", "--force-recreate",
                                   "langfuse-redis", "mcp-orchestration", "ops-controller"]


def test_recreate_takes_services_or_reading_not_both(out_dir):
    assert cli.main(["recreate", "agent", "--reading", "OPS_CONTROLLER_TOKEN", "--out", str(out_dir)]) == 1
    assert cli.main(["recreate", "--out", str(out_dir)]) == 1


def test_recreate_reading_a_key_nothing_reads_does_nothing(monkeypatch, out_dir, recorded):
    _status(monkeypatch, IDLE)
    assert cli.main(["recreate", "--reading", "NOT_READ_BY_ANYONE", "--out", str(out_dir)]) == 0
    assert recorded == []


def test_the_rendered_stack_recreates_every_holder_of_the_control_plane_token():
    """The rotation script's hand list missed mcp-orchestration (it holds the token via `secrets:`).
    Derived from the render, it cannot: every long-running reader is found, the evals one-shot is not."""
    from ordo.render.catalog import Catalog
    from ordo.render.config import Source
    from ordo.render.engine import render
    from ordo.render.plugins import PluginRegistry

    root = Path(__file__).resolve().parents[2]
    source = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                               "plugins": ["orchestration", "evals", "comfyui", "hermes-dashboard"],
                               "site": {"MEMORY_VAULT_PATH": "/srv/vault"}})
    doc = render(source, Catalog.load(root / "catalog" / "models.yaml"),
                 PluginRegistry.load(root / "services")).compose_dict()
    readers = stack.readers_of(doc, ["OPS_CONTROLLER_TOKEN"])
    assert {"ops-controller", "dashboard", "agent", "mcp-orchestration"} <= set(readers)
    assert "evals" not in readers and "evals" in doc["services"]


def test_a_status_without_the_leased_verdict_is_refused_not_guessed():
    """Every ops-controller since #230 reports the scheduler's own `leased` verdict. A status
    without it comes from an image older than that; the host no longer reconstructs the verdict
    from the raw lists (a second definition of "leased"), it refuses and says to rebuild."""
    with pytest.raises(bringup.LeaseUnknown, match="leased"):
        bringup.is_leased(LEASED_OLD_IMAGE)
    reason = bringup.lease_refusal(LEASED_OLD_IMAGE, whole_stack=True, starts=set())
    assert reason and "rebuild" in reason


def test_the_leased_verdict_alone_decides():
    assert bringup.is_leased({**IDLE, "leased": True}) is True
    assert bringup.is_leased({**LEASED, "leased": False}) is False
