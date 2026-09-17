"""The `evals` plugin render and the Hermes API-server wiring it depends on.

Both are easy to break silently: the eval runner is a ONE-SHOT container (a wrong restart policy
turns a finished run into a loop against the local model), it reads the operator's private data
through mounts that must come from BASE_PATH/DATA_PATH rather than a `./` relative path, and the
Hermes API server it drives is a terminal-capable endpoint that must stay off unless a key exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ordo import wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
SITE = {"MEMORY_VAULT_PATH": "/srv/ordo/memory-vault"}


def _src(**kw):
    base = {"hardware": P_5090, "tier": "auto", "model": "auto", "plugins": ["evals"], "site": SITE}
    base.update(kw)
    return Source.from_dict(base)


def _evals_service(rc):
    return rc.compose_dict()["services"]["evals"]


# ── opt-in gate ────────────────────────────────────────────────────────────────

def test_evals_is_opt_in():
    rc = render(Source.from_dict({"hardware": P_5090, "plugins": "auto", "site": SITE}), CATALOG, REGISTRY)
    assert "evals" not in rc.plugins_enabled
    assert "evals" not in rc.compose_dict()["services"]


def test_evals_enables_when_listed_and_rides_its_own_profile():
    rc = render(_src(), CATALOG, REGISTRY)
    assert rc.plugins_enabled == ["evals"]
    assert rc.env["EVALS_ENABLED"] == "1"
    assert _evals_service(rc)["profiles"] == ["evals"]


# ── one-shot job shape ─────────────────────────────────────────────────────────

def test_the_runner_is_a_one_shot_container_not_a_daemon():
    """`restart: no`. Under the default unless-stopped a finished run would be restarted into the
    same run forever, and a `COMPOSE_PROFILES='*' up -d` would loop it against the local model.
    (The policy itself is validated in ordo/plugins.py, tested in test_langfuse.py.)"""
    assert _evals_service(render(_src(), CATALOG, REGISTRY))["restart"] == "no"


def test_the_runner_publishes_no_host_port_and_gets_no_docker_socket():
    service = _evals_service(render(_src(), CATALOG, REGISTRY))
    assert "ports" not in service
    assert not any("docker.sock" in volume for volume in service["volumes"])
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_the_runner_requests_no_gpu():
    """No suite generates images or video; a GPU reservation here would bypass the scheduler lease."""
    service = _evals_service(render(_src(), CATALOG, REGISTRY))
    assert "deploy" not in service or "reservations" not in service["deploy"].get("resources", {})


# ── mounts ─────────────────────────────────────────────────────────────────────

def test_code_results_vault_and_hermes_home_are_mounted_the_documented_way():
    from ordo_evals.checks import VAULT_EVAL_ROOT

    service = _evals_service(render(_src(), CATALOG, REGISTRY))
    volumes = service["volumes"]
    code = next(v for v in volumes if v.endswith("/services/evals:/app:ro"))
    assert code.startswith("${BASE_PATH:?"), "tracked code must mount from BASE_PATH, never a ./ path"
    results = next(v for v in volumes if v.endswith(":/results"))
    assert results.startswith("${DATA_PATH:?"), "run outputs belong under DATA_PATH, outside git"
    assert any(v.startswith("${MEMORY_VAULT_PATH:?") and v.endswith(":/vault:ro") for v in volumes)
    assert any(v.startswith("${MEMORY_VAULT_PATH:?") and v.endswith(f"/{VAULT_EVAL_ROOT}:/vault/{VAULT_EVAL_ROOT}")
               for v in volumes), (
        f"the runner seeds and cleans up {VAULT_EVAL_ROOT}/ notes, so that ONE subfolder is writable")
    assert "hermes-home:/hermes-home:ro" in volumes


def test_evals_scratch_mount_overlaps_the_rag_watch_tree_only_because_it_is_hidden():
    """E6: rag-ingestion (services/rag/plugin.yaml) watches the WHOLE memory vault recursively, with
    no path-based exclusion in its mount - so the evals scratch folder, which lives inside that same
    vault, can only safely overlap the ingester's watch tree because its name is dot-prefixed and
    ingest.py's `_is_hidden` rule excludes any such path (tests/rag/test_ingest.py exercises that
    rule directly). This test locks the two configs to the SAME root name (VAULT_EVAL_ROOT) and
    checks the name is still hidden, so a future rename on either side cannot silently reopen the
    leak this whole fix closes."""
    from ordo_evals.checks import VAULT_EVAL_ROOT

    assert VAULT_EVAL_ROOT.startswith("."), "the eval scratch root must be a dot-prefixed hidden path"

    rc = render(_src(plugins=["evals", "rag"]), CATALOG, REGISTRY)
    compose = rc.compose_dict()

    evals_volumes = compose["services"]["evals"]["volumes"]
    scratch_mount = next(v for v in evals_volumes if v.endswith(f":/vault/{VAULT_EVAL_ROOT}"))
    assert scratch_mount == (
        "${MEMORY_VAULT_PATH:?MEMORY_VAULT_PATH must be set in ordo.yaml site}"
        f"/{VAULT_EVAL_ROOT}:/vault/{VAULT_EVAL_ROOT}")

    ingestion_volumes = compose["services"]["rag-ingestion"]["volumes"]
    vault_watch_mount = next(v for v in ingestion_volumes if v.endswith(":/watch/memory-vault:ro"))
    assert not vault_watch_mount.endswith(f"/{VAULT_EVAL_ROOT}:/watch/memory-vault:ro"), (
        "the ingester mount must not itself carve out the scratch folder: the whole point of the "
        "fix is that the GENERIC hidden-path rule excludes it, not a path-scoped mount")


def test_the_hermes_brain_volume_is_shared_read_only_with_the_agent():
    compose = render(_src(), CATALOG, REGISTRY).compose_dict()
    agent_mounts = [v for v in compose["services"]["agent"]["volumes"] if v.startswith("hermes-home:")]
    assert agent_mounts, "the agent must still own the brain volume this plugin reads"
    assert "hermes-home" in compose["volumes"]


# ── credentials ────────────────────────────────────────────────────────────────

def test_the_runner_gets_its_own_litellm_key_with_no_mcp_servers():
    rc = render(_src(), CATALOG, REGISTRY)
    grant = next(k for k in rc.litellm_keys if k["env"] == "LITELLM_KEY_EVALS")
    assert grant["models"] == ["local-chat", "local-embed"]
    assert grant["mcp_servers"] == []
    assert "LITELLM_KEY_EVALS" in rc.required_secrets


def test_every_secret_reaches_the_container_only_as_a_reference():
    rc = render(_src(), CATALOG, REGISTRY)
    environment = _evals_service(rc)["environment"]
    plugin = REGISTRY.get("evals")
    interpolation = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:?-][^}]*)?\}")
    referenced = {m.group(1) for value in environment.values() for m in interpolation.finditer(str(value))}
    assert {"LITELLM_KEY_EVALS", "HERMES_API_SERVER_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"} <= referenced
    for key, value in environment.items():
        if re.search(r"(KEY|TOKEN|SECRET)$", key):
            assert str(value).startswith("${"), f"{key} holds a literal instead of a ${{...}} reference"
    for secret in plugin.secrets:
        assert secret in rc.required_secrets, f"{secret} would be missing from secrets.env.example"
    values = {key: wizard.generator_for(key)() for key in ("HERMES_API_SERVER_KEY", "LITELLM_KEY_EVALS")}
    text = yaml.safe_dump(rc.compose_dict(), sort_keys=False)
    for key, value in values.items():
        assert value not in text, f"the VALUE of {key} was inlined into the rendered compose"


def test_the_api_server_key_has_a_strong_generator():
    """Hermes refuses to start its API server on a key shorter than 16 characters, and a holder of
    the key can run the agent with its full toolset."""
    assert wizard.generator_for("HERMES_API_SERVER_KEY") is not None
    assert len(wizard.SECRET_GENERATORS["HERMES_API_SERVER_KEY"]()) >= 32


def test_the_api_server_key_is_rotatable():
    script = (ROOT / "scripts" / "secrets" / "rotate-internal.sh").read_text(encoding="utf-8")
    assert 'print "HERMES_API_SERVER_KEY"' in script


# ── Hermes API server wiring ───────────────────────────────────────────────────

def test_hermes_maps_the_key_onto_api_server_key_with_an_empty_fallback():
    """Off by default: with the evals plugin disabled the ref interpolates to "" and Hermes never
    enrols the platform. Mapped straight onto API_SERVER_KEY because Hermes strips that name from
    every terminal-tool subprocess environment - a second copy would be readable by the agent."""
    agent = yaml.safe_load((ROOT / "services" / "hermes" / "agent.yaml").read_text(encoding="utf-8"))
    environment = agent["environment"]
    assert environment["API_SERVER_KEY"] == "${HERMES_API_SERVER_KEY:-}"
    assert environment["API_SERVER_HOST"] == "0.0.0.0"
    assert environment["API_SERVER_PORT"] == "8642"
    assert not any(str(value).strip() == "${HERMES_API_SERVER_KEY}" for value in environment.values()), (
        "the key must not also reach the agent under a name Hermes does not strip from tool subprocesses")


def test_the_entrypoint_disables_the_api_server_without_a_usable_key():
    entrypoint = (ROOT / "services" / "hermes" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "unset API_SERVER_KEY API_SERVER_HOST API_SERVER_PORT" in entrypoint
    assert '"${#API_SERVER_KEY}" -ge 16' in entrypoint, "Hermes's own startup guard rejects shorter keys"


def test_the_agent_publishes_no_host_port_for_the_api_server():
    """The endpoint dispatches terminal-capable agent work: it stays on the project network, with no
    host publish and no Caddy route."""
    compose = render(_src(), CATALOG, REGISTRY).compose_dict()
    assert "ports" not in compose["services"]["agent"]
    caddyfile = (ROOT / "auth" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "8642" not in caddyfile


def test_the_runner_dials_hermes_and_the_gateway_on_the_project_network():
    environment = _evals_service(render(_src(), CATALOG, REGISTRY))["environment"]
    assert environment["HERMES_API_URL"] == "http://agent:8642/v1"
    assert environment["MODEL_BASE_URL"] == "http://model-gateway:11435/v1"
    assert environment["LANGFUSE_HOST"] == "http://langfuse-web:3000"
    assert environment["MODEL_NAME"] == "local-chat"


# ── E7: git provenance passthrough ──────────────────────────────────────────────

def test_git_provenance_env_vars_pass_through_from_the_invoking_shell():
    """GIT_COMMIT/GIT_DIRTY have no baked-in value: the image can't compute them (no git binary),
    so they must interpolate from whatever the invoking shell exported (scripts/evals/run.sh),
    empty when it did not (a bare `docker compose run`) - ordo_evals.settings reads empty as
    provenance-unknown and runner._provenance_gate refuses to start without --allow-dirty."""
    environment = _evals_service(render(_src(), CATALOG, REGISTRY))["environment"]
    assert environment["GIT_COMMIT"] == "${GIT_COMMIT:-}"
    assert environment["GIT_DIRTY"] == "${GIT_DIRTY:-}"


def test_run_sh_computes_provenance_with_the_real_host_git_and_forwards_args():
    """scripts/evals/run.sh is the canonical invocation (services/evals/README.md's E7 section):
    it must compute GIT_COMMIT/GIT_DIRTY with the real git (the container has none), scope the
    dirty check to services/evals (unrelated repo changes must not block a run), export both before
    docker compose starts, and forward every CLI argument through untouched."""
    script = (ROOT / "scripts" / "evals" / "run.sh").read_text(encoding="utf-8")
    assert 'git -C "$REPO_ROOT" rev-parse HEAD' in script
    assert 'git -C "$REPO_ROOT" status --porcelain -- services/evals' in script
    assert "export GIT_COMMIT GIT_DIRTY" in script
    assert 'run --rm evals "$@"' in script
