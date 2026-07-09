"""Unit tests for the ops-api SAFE per-service recreate command builder.

`compose_recreate.build_recreate_cmd` is the ONLY thing that shells docker-compose against
the ordo-v2 stack for a dashboard-button recreate. These tests pin its exact argv so a
regression (missing secrets.env, a stray dep, a whole-stack up) is caught offline — the
guardrails the 2026-06-26 secret-less-recreate and the llamacpp pin-drop incidents demand.

The module is pure (no fastapi/docker imports), so it loads directly via importlib from the
ops-api build context without the substrate's dev deps needing the runtime deps.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_CR_PATH = ROOT / "docker" / "ops-api" / "compose_recreate.py"


def _load():
    spec = importlib.util.spec_from_file_location("compose_recreate", _CR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CR = _load()


def _cmd(service="llamacpp", profiles=None):
    return CR.build_recreate_cmd(
        service, project="ordo-v2", project_dir="/workspace",
        compose_files=["docker-compose.yml"], profiles=profiles,
    )


def test_recreate_cmd_targets_the_ordo_v2_project():
    cmd = _cmd()
    assert cmd[0] == "docker-compose"
    # project pinned to ordo-v2 — never another project
    assert cmd[cmd.index("--project-name") + 1] == "ordo-v2"
    # project-directory is the mounted rendered out/ tree (where .env/secrets.env live)
    assert cmd[cmd.index("--project-directory") + 1] == "/workspace"


def test_recreate_cmd_passes_BOTH_env_files():
    # the 2026-06-26 regression check: secrets.env MUST be present, and .env too (passing any
    # --env-file disables compose's implicit .env auto-load, so .env has to be listed explicitly).
    cmd = _cmd()
    env_flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--env-file"]
    assert env_flags == ["/workspace/.env", "/workspace/secrets.env"]
    assert cmd.count("--env-file") == 2


def test_recreate_cmd_is_no_deps_and_force_recreate():
    cmd = _cmd()
    assert "--no-deps" in cmd            # ONLY the named service — never cascade its deps
    assert "--force-recreate" in cmd     # restart even when compose is unchanged (.env edit)
    # it is an `up`, never a whole-stack `down`/`restart`
    assert "up" in cmd and "down" not in cmd and "restart" not in cmd


def test_recreate_cmd_names_only_the_requested_service_last():
    cmd = _cmd("open-webui")
    # exactly one service argument, and it is the requested one (trailing positional)
    assert cmd[-1] == "open-webui"
    for svc in ("llamacpp", "model-gateway", "ops-controller", "caddy", "oauth2-proxy"):
        assert svc not in cmd  # no OTHER service is ever named (no dep cascade)


def test_recreate_cmd_references_the_compose_file_in_the_project_dir():
    cmd = _cmd()
    assert cmd[cmd.index("-f") + 1] == "/workspace/docker-compose.yml"


def test_recreate_cmd_passes_every_profile_so_profiled_deps_resolve():
    # a target service's depends_on may reference a profiled peer (open-webui -> qdrant behind
    # `rag`); each profile the stack runs with must be passed or compose aborts "no such service".
    cmd = _cmd("open-webui", profiles=["rag", "webui", "monitoring"])
    prof_flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--profile"]
    assert prof_flags == ["rag", "webui", "monitoring"]
    # profiles widen resolution only; --no-deps still scopes the recreate to the one service
    assert "--no-deps" in cmd and cmd[-1] == "open-webui"


def test_discover_profiles_collects_and_dedupes_from_compose():
    compose = {"services": {
        "open-webui": {"profiles": ["webui"]},
        "qdrant": {"profiles": ["rag"]},
        "rag-ingestion": {"profiles": ["rag"]},          # dup 'rag' -> deduped
        "llamacpp": {},                                   # no profiles key
        "grafana": {"profiles": ["monitoring"]},
    }}
    assert CR.discover_profiles(compose) == ["monitoring", "rag", "webui"]


def test_discover_profiles_empty_compose_is_empty_list():
    assert CR.discover_profiles({}) == []
    assert CR.discover_profiles({"services": {}}) == []


def test_recreate_cmd_rejects_empty_service():
    with pytest.raises(ValueError):
        CR.build_recreate_cmd("", project="ordo-v2", project_dir="/workspace",
                              compose_files=["docker-compose.yml"])


def test_env_files_constant_is_dot_env_then_secrets():
    # order matters: .env (derived) first, secrets.env (operator secrets) second — later wins.
    assert CR.ENV_FILES == (".env", "secrets.env")


# ── gate defaults (source-level guard; the live default is verified in validation) ──────────────
_MAIN_SRC = (ROOT / "docker" / "ops-api" / "main.py").read_text(encoding="utf-8")


def test_service_recreate_gate_defaults_off_in_source():
    # OPS_SERVICE_RECREATE_ENABLED must default to "0" so the substrate default + CI stay safe;
    # ONLY this deployment's manifest flips it to "1".
    assert 'os.environ.get(\n    "OPS_SERVICE_RECREATE_ENABLED", "0"' in _MAIN_SRC


def test_compose_mutations_gate_still_defaults_off_in_source():
    # whole-stack /compose/* stays default-off (unchanged) — the scheduler owns the stack lifecycle.
    assert 'os.environ.get(\n    "OPS_COMPOSE_MUTATIONS_ENABLED", "0"' in _MAIN_SRC


def test_compose_endpoints_still_501_when_mutations_disabled_in_source():
    # /compose/{up,down,restart} gate ONLY on OPS_COMPOSE_MUTATIONS_ENABLED and raise 501 — the
    # per-service recreate gate must NOT have opened them (whole-stack mutations stay disabled).
    for handler in ("compose_up", "compose_down", "compose_restart"):
        assert f"async def {handler}(" in _MAIN_SRC
    # each compose endpoint 501s on the whole-stack switch, and none reference the per-service gate
    assert _MAIN_SRC.count("if not OPS_COMPOSE_MUTATIONS_ENABLED:  # V2 PATCH: scheduler owns compose lifecycle") == 3
