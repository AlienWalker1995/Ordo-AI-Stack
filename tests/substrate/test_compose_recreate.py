"""The per-service recreate argv, which is the only thing that shells compose against the stack.

These pin `DockerBackend.recreate_service`'s exact command so a regression (a missing
`secrets.env`, a dropped `--no-deps` that cascade-recreates dependencies, a whole-stack `up`) is
caught offline. They are the guardrails the 2026-06-26 secret-less-recreate and the llamacpp
pin-drop incidents demand, carried over from the retired ops-api's `compose_recreate` module when
the verb moved onto the v2 control plane.

The command is captured rather than run: `subprocess.run` is patched, so nothing touches docker.
"""
from pathlib import Path

import pytest
import yaml

from ordo.control.broker import DockerBackend

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """A backend whose compose dir is a tiny rendered stack, returning the argv it would run."""
    compose = {
        "services": {
            "llamacpp": {"image": "x"},
            "open-webui": {"image": "x", "profiles": ["webui"]},
            "qdrant": {"image": "x", "profiles": ["rag"]},
            "prometheus": {"image": "x", "profiles": ["monitoring"]},
        }
    }
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(compose), encoding="utf-8")

    backend = DockerBackend("ordo")
    backend.COMPOSE_DIR = str(tmp_path)
    recorded: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        recorded.append(list(cmd))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("ordo.control.broker.subprocess.run", fake_run)
    return backend, recorded


def _recreate(capture, service="llamacpp"):
    backend, recorded = capture
    backend.recreate_service(service)
    return recorded[-1]


def test_recreate_targets_the_ordo_project(capture):
    cmd = _recreate(capture)
    assert cmd[:2] == ["docker", "compose"]
    assert cmd[cmd.index("-p") + 1] == "ordo"


def test_recreate_passes_BOTH_env_files(capture):
    """The 2026-06-26 regression: secrets.env must be there, and so must .env.

    Passing any --env-file disables compose's implicit .env auto-load, so omitting .env leaves
    every derived value unset just as surely as omitting secrets.env leaves every secret unset.
    """
    backend, _ = capture
    cmd = _recreate(capture)
    env_flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--env-file"]
    assert env_flags == [f"{backend.COMPOSE_DIR}/.env", f"{backend.COMPOSE_DIR}/secrets.env",
                         f"{backend.COMPOSE_DIR}/secret-files.env"]


def test_recreate_is_no_deps_and_force_recreate(capture):
    cmd = _recreate(capture)
    assert "--no-deps" in cmd          # ONLY the named service, never a cascade onto its deps
    assert "--force-recreate" in cmd   # restart even when the compose file is unchanged
    assert "up" in cmd
    assert "down" not in cmd and "restart" not in cmd


def test_recreate_names_only_the_requested_service(capture):
    cmd = _recreate(capture, "open-webui")
    assert cmd[-1] == "open-webui"
    for other in ("llamacpp", "model-gateway", "ops-controller", "caddy", "oauth2-proxy"):
        assert other not in cmd


def test_recreate_references_the_compose_file_in_the_project_dir(capture):
    backend, _ = capture
    cmd = _recreate(capture)
    assert cmd[cmd.index("-f") + 1] == f"{backend.COMPOSE_DIR}/docker-compose.yml"


def test_recreate_passes_every_profile_so_profiled_deps_resolve(capture):
    """A target's depends_on may name a profiled peer (open-webui -> qdrant, behind `rag`).

    Every profile the stack defines must be passed or compose aborts with "no such service".
    Widening the resolvable set is safe; --no-deps is what keeps the recreate to one service.
    """
    cmd = _recreate(capture, "open-webui")
    profiles = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--profile"]
    assert profiles == ["monitoring", "rag", "webui"]


def test_profiles_are_read_from_the_rendered_compose_not_a_hardcoded_list(capture):
    """A new profiled plugin must not need an edit here to be recreatable."""
    backend, _ = capture
    path = Path(backend.COMPOSE_DIR) / "docker-compose.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["services"]["newthing"] = {"image": "x", "profiles": ["brand-new"]}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert "brand-new" in backend._profiles()


def test_a_service_name_that_would_escape_the_project_is_refused(capture):
    backend, recorded = capture
    with pytest.raises(ValueError):
        backend.recreate_service("../other-project/thing")
    assert recorded == []


def test_the_real_rendered_stack_has_profiles_to_pass():
    """Guards the fixture against being the only place profiles exist."""
    rendered = ROOT / "out" / "docker-compose.yml"
    if not rendered.exists():
        pytest.skip("no rendered stack in this checkout")
    doc = yaml.safe_load(rendered.read_text(encoding="utf-8")) or {}
    profiles = {
        p
        for service in (doc.get("services") or {}).values()
        for p in (service or {}).get("profiles") or []
    }
    assert profiles, "the rendered stack defines no profiles, so the --profile flags are untested"


def test_a_named_compose_up_never_starts_the_services_dependencies(capture):
    # `up -d model-gateway` without --no-deps also starts llamacpp (its dependency). During a GPU
    # lease llamacpp is evicted, and the lease guard only checks the NAMED service, so that
    # call would put the resident back on the leased card beside the render.
    backend, recorded = capture
    backend.compose_up("open-webui")
    cmd = recorded[-1]
    assert cmd[-4:] == ["up", "-d", "--no-deps", "open-webui"]


def test_a_whole_stack_compose_up_is_unchanged(capture):
    backend, recorded = capture
    backend.compose_up()
    assert recorded[-1][-2:] == ["up", "-d"]
