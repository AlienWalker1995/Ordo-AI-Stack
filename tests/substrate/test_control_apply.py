"""ops-controller's post-render step: after a source write and a render, recreate exactly the changed set.

Every caller that changes the operator source through the control plane (the model switch, a plugin
enable or disable, the dashboard's MCP toggle) used to keep its own hand-written list of what to
restart afterwards. The dashboard's said "llamacpp + model-gateway (+ llamacpp-cpu)" and reported
"restart Hermes" on a context change, because the agent reads LLAMACPP_CTX_SIZE too. The render
already decides what changed: `ordo/render/changed_set.py` compares every rendered service's config
hash and image with its container's, the same computation the host's `ordo apply` makes.

These tests drive the real render against a fake docker whose containers were created from an
earlier render (a config hash here is the sha256 of the service definition with the rendered .env
interpolated, the way compose hashes after interpolation). So "what changed" is whatever the render
says, not what the test claims.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Job, Scheduler
from ordo.render.catalog import Catalog
from ordo.render.changed_set import RenderedService, RunningContainer, StackState
from ordo.render.plugins import PluginRegistry
from ordo.render.stack import expand_env, load_compose, plan_named

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
COMPOSE_VERSION = "5.1.0"
HARDWARE = {"gpus": [{"vram_gb": 32}], "ram_gb": 128}
# Two catalog models whose context windows differ, and a third with the same window as the second.
SMALL_CTX_MODEL = "qwen2.5-7b-instruct-q4"          # 65536
LARGE_CTX_MODEL = "qwen3.8-27b-q6"                  # 131072
SAME_CTX_MODEL = "qwen3.8-27b-uncensored-q6"        # 131072
TOKEN = "apply-test-token-7c2a"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Actor": "dashboard"}


def _env(out: Path) -> dict[str, str]:
    env = {}
    for line in (out / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    return env


class RenderedStackBackend(MockBackend):
    """A docker whose containers are whatever `create_all` saw in out/, and whose rendered side is
    whatever out/ holds now. A recreate brings the named services (and their netns members) to the
    current render, the way `up -d --no-deps --force-recreate` does."""

    def __init__(self, out: Path):
        super().__init__()
        self.out = out
        self.containers: dict[str, RunningContainer] = {}
        self.fail_recreate_after: int | None = None     # recreate this many, then raise
        self.state_error: Exception | None = None
        self.uncached_repos: set[str] = set()           # image repos the local cache lacks

    def _rendered(self) -> dict[str, RenderedService]:
        env = _env(self.out)
        rendered = {}
        for name, spec in (load_compose(str(self.out)).get("services") or {}).items():
            text = expand_env(json.dumps(spec, sort_keys=True), env)
            image = expand_env(str(spec.get("image") or ""), env)
            rendered[name] = RenderedService(service=name, config_hash=hashlib.sha256(text.encode()).hexdigest(),
                                             image_ref=image,
                                             image_id=None if image.rsplit(":", 1)[0] in self.uncached_repos
                                             else f"id:{image}",
                                             one_shot=str(spec.get("restart")) == "no")
        return rendered

    def _create(self, service: RenderedService) -> None:
        self.containers[service.service] = RunningContainer(
            service=service.service, config_hash=service.config_hash, image_id=service.image_id or "",
            compose_version=COMPOSE_VERSION, container_id=f"cid-{service.service}")

    def create_all(self) -> None:
        for service in self._rendered().values():
            if not service.one_shot:
                self._create(service)

    def rendered_compose(self) -> dict:
        return load_compose(str(self.out))

    def stack_state(self) -> StackState:
        if self.state_error is not None:
            raise self.state_error
        return StackState(rendered=self._rendered(), running=dict(self.containers),
                          compose_version=COMPOSE_VERSION)

    def recreate_services(self, services: list[str]) -> None:
        self.recreate_batches.append(list(services))
        _args, targets = plan_named(self.rendered_compose(), services, force_recreate=True)
        rendered = self._rendered()
        for count, name in enumerate(sorted(targets)):
            if self.fail_recreate_after is not None and count >= self.fail_recreate_after:
                self.fail_recreate_after = None
                raise RuntimeError(f"docker compose up failed while recreating {name}")
            self._create(rendered[name])

    def stop(self, service: str) -> None:
        super().stop(service)
        self.containers.pop(service, None)

    def remove_stopped_containers(self, services: list[str]) -> None:
        super().remove_stopped_containers(services)
        for name in services:
            self.containers.pop(name, None)


def _write_source(path: Path, **fields) -> None:
    path.write_text(yaml.safe_dump({"hardware": HARDWARE, "plugins": "auto", **fields}, sort_keys=False),
                    encoding="utf-8")


@pytest.fixture
def stack(tmp_path):
    """A control plane whose stack runs the render of `model: SMALL_CTX_MODEL`."""
    source = tmp_path / "ordo.yaml"
    out = tmp_path / "out"
    _write_source(source, model=SMALL_CTX_MODEL)
    backend = RenderedStackBackend(out)
    scheduler = Scheduler(32)
    cp = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=Broker(scheduler, backend))
    cp._render().write(out)
    backend.create_all()
    return cp, backend, source, out


def _recreated(backend: RenderedStackBackend) -> list[str]:
    return sorted(name for batch in backend.recreate_batches for name in batch)


# --------------------------------------------------------------------------- #
# The model switch.
# --------------------------------------------------------------------------- #


def test_a_switch_that_changes_the_context_recreates_what_the_render_changed(stack):
    cp, backend, _, _ = stack
    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 200, body
    applied = body["apply"]
    # The chat server loads the new file, the gateway re-templates its aliases, and the CPU
    # fallback's window mirrors the GPU's: the render changed all three.
    assert applied["recreated"] == ["llamacpp", "llamacpp-cpu", "model-gateway"]
    assert _recreated(backend) == ["llamacpp", "llamacpp-cpu", "model-gateway"]
    # The agent reads LLAMACPP_CTX_SIZE too, and it is the process calling this API: it cannot be
    # cycled from here, so the host command that finishes the switch is named precisely.
    assert applied["restart_required_on_host"] == ["agent"]
    assert applied["host_command"] == "ordo apply --only agent"
    assert "control plane" in applied["host_reasons"]["agent"]


def test_a_switch_with_the_same_context_leaves_the_fallback_and_the_agent_alone(stack):
    cp, backend, source, out = stack
    _write_source(source, model=SAME_CTX_MODEL)
    cp._render().write(out)
    backend.create_all()
    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 200, body
    assert body["apply"]["recreated"] == ["llamacpp", "model-gateway"]
    assert body["apply"]["restart_required_on_host"] == [] and body["apply"]["host_command"] is None


def test_unchanged_services_are_never_touched(stack):
    cp, backend, _, _ = stack
    before = dict(backend.containers)
    cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    touched = {name for name in before if backend.containers[name] != before[name]}
    assert touched == {"llamacpp", "llamacpp-cpu", "model-gateway"}
    assert backend.started == backend.stopped == backend.restarted == []


def test_a_switch_to_the_running_model_recreates_nothing(stack):
    cp, backend, _, _ = stack
    status, body = cp.route("POST", "/model-config", {"model": SMALL_CTX_MODEL})
    assert status == 200, body
    assert body["apply"]["recreated"] == [] and backend.recreate_batches == []


def test_a_lease_refusal_propagates_and_rolls_the_source_back(stack):
    cp, backend, source, out = stack
    before_source = source.read_text(encoding="utf-8")
    before_env = (out / ".env").read_text(encoding="utf-8")
    # A render holds the card and llama.cpp was evicted to make room for it: recreating llama.cpp
    # now would put a second tenant on a leased GPU (the 2026-08-08 host crash).
    cp.scheduler.cache_idle("llamacpp", 25)
    cp.broker.request(Job("gate-comfyui", 20, "media"))
    assert "llamacpp" in cp.scheduler.evicted_residents

    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 409
    assert "gate-comfyui" in body["error"] and "llamacpp" in body["error"]
    assert backend.recreate_batches == []
    assert body["rolled_back"] is True
    assert source.read_text(encoding="utf-8") == before_source
    assert (out / ".env").read_text(encoding="utf-8") == before_env


def test_a_failed_recreate_restores_the_source_and_re_applies_it(stack):
    cp, backend, source, out = stack
    before_source = source.read_text(encoding="utf-8")
    before = dict(backend.containers)
    backend.fail_recreate_after = 1        # the first service is recreated, then compose fails

    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 500
    assert "docker compose up failed" in body["error"]
    assert body["rolled_back"] is True
    assert source.read_text(encoding="utf-8") == before_source
    # The rollback re-applies the previous render: the one service already recreated onto the new
    # model is recreated back, so nothing is left running a config the source no longer names.
    assert body["rollback"]["recreated"] == ["llamacpp"]
    assert backend.containers == before


def test_an_unreadable_docker_state_refuses_and_rolls_back(stack):
    from ordo.render.changed_set import StateUnknown

    cp, backend, source, _ = stack
    before_source = source.read_text(encoding="utf-8")
    backend.state_error = StateUnknown("`docker ps -a` failed: permission denied")
    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 503
    assert "permission denied" in body["error"]
    assert source.read_text(encoding="utf-8") == before_source
    assert backend.recreate_batches == []


def test_a_container_made_by_another_compose_version_is_left_to_the_host(stack):
    cp, backend, _, _ = stack
    old = backend.containers["llamacpp-cpu"]
    backend.containers["llamacpp-cpu"] = RunningContainer(
        service=old.service, config_hash=old.config_hash, image_id=old.image_id,
        compose_version="2.30.0", container_id=old.container_id)
    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 200, body
    assert "llamacpp-cpu" not in body["apply"]["recreated"]
    assert body["apply"]["restart_required_on_host"] == ["agent", "llamacpp-cpu"]
    assert "compose" in body["apply"]["host_reasons"]["llamacpp-cpu"]
    assert body["apply"]["host_command"] == "ordo apply --only agent llamacpp-cpu"


# --------------------------------------------------------------------------- #
# Plugin enable / disable (the MCP toggle goes through these).
# --------------------------------------------------------------------------- #


@pytest.fixture
def listed(tmp_path):
    """A stack whose source lists its plugins explicitly (searxng-web only)."""
    source = tmp_path / "ordo.yaml"
    out = tmp_path / "out"
    _write_source(source, model=SMALL_CTX_MODEL, plugins=["searxng-web"])
    backend = RenderedStackBackend(out)
    scheduler = Scheduler(32)
    cp = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=Broker(scheduler, backend))
    cp._render().write(out)
    backend.create_all()
    return cp, backend, source, out


def test_enabling_an_mcp_plugin_creates_its_server_and_recreates_the_gateway(listed):
    cp, backend, _, _ = listed
    status, body = cp.route("POST", "/plugins/searxng/enable", {"confirm": True})
    assert status == 200, body
    # LiteLLM reads its MCP servers at startup: the gateway (and the key bootstrap reading the same
    # rendered directory) are in the changed set because their definition carries its digest.
    assert body["apply"]["recreated"] == ["mcp-searxng", "model-gateway", "model-gateway-keys"]
    assert body["apply"]["restart_required_on_host"] == []


def test_disabling_an_mcp_plugin_stops_its_server_and_recreates_the_gateway(listed):
    cp, backend, _, _ = listed
    cp.route("POST", "/plugins/searxng/enable", {"confirm": True})
    backend.recreate_batches.clear()
    status, body = cp.route("POST", "/plugins/searxng/disable", {"confirm": True})
    assert status == 200, body
    assert body["apply"]["stopped"] == ["mcp-searxng"]
    assert backend.stopped == ["mcp-searxng"]
    assert body["apply"]["recreated"] == ["model-gateway", "model-gateway-keys"]


def test_a_plugin_whose_secrets_are_missing_is_rendered_but_left_to_the_host(listed):
    cp, backend, _, out = listed
    assert not (out / "secrets.env").exists()     # nothing materialized: LITELLM_KEY_AUTOMATION is unset
    status, body = cp.route("POST", "/plugins/automation/enable", {"confirm": True})
    assert status == 200, body
    applied = body["apply"]
    # Started without its secret it would crash-loop: rendered, never started, escalated to the
    # host, where `ordo secrets set` supplies it and `ordo apply` starts it.
    assert "n8n" not in applied["recreated"] and "n8n" not in backend.containers
    assert applied["restart_required_on_host"] == ["n8n"]
    assert "LITELLM_KEY_AUTOMATION" in applied["host_reasons"]["n8n"]
    assert applied["host_command"] == "ordo apply --only n8n"


def test_a_plugin_whose_secrets_are_present_is_started(listed):
    cp, backend, _, out = listed
    (out / "secrets.env").write_text("LITELLM_KEY_AUTOMATION=sk-test\n", encoding="utf-8")
    status, body = cp.route("POST", "/plugins/automation/enable", {"confirm": True})
    assert status == 200, body
    assert "n8n" in body["apply"]["recreated"] and "n8n" in backend.containers
    assert body["apply"]["restart_required_on_host"] == []


def test_disable_under_plugins_auto_is_refused_and_changes_nothing(stack):
    # `plugins: auto` enables every fitting plugin, so there is no list item to remove: stopping
    # the container would only last until the next apply recreated it.
    cp, backend, source, _ = stack
    before = source.read_text(encoding="utf-8")
    status, body = cp.route("POST", "/plugins/searxng/disable", {"confirm": True})
    assert status == 409
    assert "explicit `plugins:` list" in body["error"]
    assert backend.stopped == [] and backend.recreate_batches == []
    assert source.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# POST /apply.
# --------------------------------------------------------------------------- #


def test_apply_dry_run_plans_without_touching_anything(stack):
    cp, backend, source, out = stack
    _write_source(source, model=LARGE_CTX_MODEL)
    cp._render().write(out)                      # a host render the stack has not caught up with
    status, body = cp.route("POST", "/apply", {"dry_run": True})
    assert status == 200, body
    assert body["dry_run"] is True
    assert body["recreated"] == ["llamacpp", "llamacpp-cpu", "model-gateway"]
    assert {c["service"] for c in body["changes"]} == {"agent", "llamacpp", "llamacpp-cpu", "model-gateway"}
    assert backend.recreate_batches == []


def test_apply_needs_confirmation_unless_it_is_a_dry_run(stack):
    cp, backend, _, _ = stack
    status, _ = cp.route("POST", "/apply", {})
    assert status == 400
    assert backend.recreate_batches == []


def test_apply_recreates_the_changed_set_of_the_current_render(stack):
    cp, backend, source, out = stack
    _write_source(source, model=LARGE_CTX_MODEL)
    cp._render().write(out)
    status, body = cp.route("POST", "/apply", {"confirm": True})
    assert status == 200, body
    assert body["dry_run"] is False
    assert _recreated(backend) == ["llamacpp", "llamacpp-cpu", "model-gateway"]
    assert body["restart_required_on_host"] == ["agent"]


@pytest.fixture
def evals_stack(tmp_path):
    """A stack that runs the evals plugin (a one-shot job, `restart: "no"`)."""
    source = tmp_path / "ordo.yaml"
    out = tmp_path / "out"
    _write_source(source, model=SMALL_CTX_MODEL, plugins=["evals"], site={"MEMORY_VAULT_PATH": "/srv/vault"})
    backend = RenderedStackBackend(out)
    scheduler = Scheduler(32)
    cp = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=Broker(scheduler, backend))
    cp._render().write(out)
    backend.create_all()
    return cp, backend


def _stale_evals_container(backend: RenderedStackBackend, state: str) -> None:
    """An evals container created from an earlier render: its image moved on since."""
    evals = backend._rendered()["evals"]
    backend.containers["evals"] = RunningContainer(
        service="evals", config_hash=evals.config_hash, image_id="id:ordo/evals:older",
        compose_version=COMPOSE_VERSION, container_id="cid-evals", state=state)


def test_apply_removes_a_stale_stopped_job_container_and_reports_it(evals_stack):
    """Audit D8-1: after a render the evals container sat in `Created` on the old image."""
    cp, backend = evals_stack
    _stale_evals_container(backend, "created")
    status, body = cp.route("POST", "/apply", {"confirm": True})
    assert status == 200, body
    assert body["removed_jobs"] == ["evals"] and body["running_jobs"] == []
    assert backend.removed_containers == [["evals"]]
    assert "evals" not in backend.containers
    assert "evals" not in _recreated(backend) and "evals" not in backend.started


def test_apply_dry_run_lists_a_stale_job_container_and_removes_nothing(evals_stack):
    cp, backend = evals_stack
    _stale_evals_container(backend, "exited")
    status, body = cp.route("POST", "/apply", {"dry_run": True})
    assert status == 200, body
    assert body["removed_jobs"] == ["evals"]
    assert backend.removed_containers == [] and "evals" in backend.containers


def test_apply_never_removes_a_running_job_container(evals_stack):
    cp, backend = evals_stack
    _stale_evals_container(backend, "running")
    status, body = cp.route("POST", "/apply", {"confirm": True})
    assert status == 200, body
    assert body["removed_jobs"] == [] and body["running_jobs"] == ["evals"]
    assert backend.removed_containers == [] and "evals" in backend.containers


def test_apply_is_bearer_protected_and_audited(stack, monkeypatch, tmp_path):
    cp, _, _, _ = stack
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("ordo.control.api.AUDIT_LOG_PATH", audit_path)
    client = TestClient(cp.app(auth_token=TOKEN), raise_server_exceptions=False)
    assert client.post("/apply", json={"dry_run": True}).status_code == 401
    response = client.post("/apply", json={"dry_run": True}, headers=AUTH)
    assert response.status_code == 200, response.text
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    # The unauthenticated call's body is never read, so its record cannot say it was a dry run.
    assert [(r["action"], r["target"], r["result"], r["dry_run"]) for r in records] == [
        ("apply", "stack", "refused", False), ("apply", "stack", "ok", True)]
    assert records[1]["caller"] == "dashboard"


def test_apply_without_a_container_backend_is_unavailable(tmp_path):
    source = tmp_path / "ordo.yaml"
    _write_source(source, model=SMALL_CTX_MODEL)
    cp = ControlPlane(source, CATALOG, REGISTRY, tmp_path / "out")
    status, _ = cp.route("POST", "/apply", {"dry_run": True})
    assert status == 503


# --------------------------------------------------------------------------- #
# A service recreated onto a model file the models volume lacks crash-loops; the host fetches.
# --------------------------------------------------------------------------- #


def _listed_with_volume(tmp_path, files):
    """The `listed` stack, with a models volume that holds `files` (None: it cannot be listed)."""
    source = tmp_path / "ordo.yaml"
    out = tmp_path / "out"
    _write_source(source, model=SMALL_CTX_MODEL, plugins=["searxng-web"])
    backend = RenderedStackBackend(out)
    scheduler = Scheduler(32)
    cp = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=Broker(scheduler, backend),
                      model_volume_files=lambda: files)
    cp._render().write(out)
    backend.create_all()
    return cp, backend


def _embed_file(cp) -> str:
    from ordo.render.models_volume import required_model_files
    rc = cp._render()
    return next(f.file for f in required_model_files(rc.compose_dict(), rc.env, ["llamacpp-embed"]))


def test_a_plugin_whose_model_file_is_missing_is_left_to_the_host_to_fetch(tmp_path):
    cp, backend = _listed_with_volume(tmp_path, set())
    status, body = cp.route("POST", "/plugins/rag/enable", {"confirm": True})
    assert status == 200, body
    applied = body["apply"]
    assert "llamacpp-embed" not in applied["recreated"] and "llamacpp-embed" not in backend.containers
    assert "llamacpp-embed" in applied["restart_required_on_host"]
    assert _embed_file(cp) in applied["host_reasons"]["llamacpp-embed"]
    assert "llamacpp-embed" in applied["host_command"]


def test_a_plugin_whose_model_file_is_present_is_started(tmp_path):
    files: set[str] = set()
    cp, backend = _listed_with_volume(tmp_path, files)
    source = yaml.safe_load(cp.source_path.read_text(encoding="utf-8"))
    source["plugins"] = ["searxng-web", "rag"]
    from ordo.render.config import Source
    from ordo.render.engine import render
    rc = render(Source.from_dict(source), CATALOG, REGISTRY)
    from ordo.render.models_volume import required_model_files
    files.update(f.file for f in required_model_files(rc.compose_dict(), rc.env, ["llamacpp-embed"]))
    status, body = cp.route("POST", "/plugins/rag/enable", {"confirm": True})
    assert status == 200, body
    assert "llamacpp-embed" in body["apply"]["recreated"] and "llamacpp-embed" in backend.containers


def test_an_unlistable_models_volume_leaves_model_loaders_to_the_host(tmp_path):
    cp, backend = _listed_with_volume(tmp_path, None)
    status, body = cp.route("POST", "/plugins/rag/enable", {"confirm": True})
    assert status == 200, body
    applied = body["apply"]
    assert "llamacpp-embed" not in backend.containers
    assert "cannot list the models volume" in applied["host_reasons"]["llamacpp-embed"]


# --------------------------------------------------------------------------- #
# A first-party image is built from this checkout, never pulled: an unbuilt one waits for the host.
# --------------------------------------------------------------------------- #


def _rag_ready(tmp_path):
    """The `listed` stack with every model file present, so only images decide what rag's enable does."""
    files: set[str] = set()
    cp, backend = _listed_with_volume(tmp_path, files)
    source = yaml.safe_load(cp.source_path.read_text(encoding="utf-8"))
    source["plugins"] = ["searxng-web", "rag"]
    from ordo.render.config import Source
    from ordo.render.engine import render
    from ordo.render.models_volume import required_model_files
    rc = render(Source.from_dict(source), CATALOG, REGISTRY)
    files.update(f.file for f in required_model_files(rc.compose_dict(), rc.env, list(rc.compose_dict()["services"])))
    return cp, backend


def test_an_unbuilt_first_party_image_is_left_to_the_host_to_build(tmp_path):
    cp, backend = _rag_ready(tmp_path)
    backend.uncached_repos.add("ordo/rag-ingestion")
    status, body = cp.route("POST", "/plugins/rag/enable", {"confirm": True})
    assert status == 200, body
    applied = body["apply"]
    assert "rag-ingestion" not in applied["recreated"] and "rag-ingestion" not in backend.containers
    assert "ordo/rag-ingestion" in applied["host_reasons"]["rag-ingestion"]
    assert "rag-ingestion" in applied["host_command"]
    assert "qdrant" in applied["recreated"]          # the rest of the plugin still starts


def test_an_uncached_third_party_image_is_pulled_as_before(tmp_path):
    cp, backend = _rag_ready(tmp_path)
    backend.uncached_repos.add("qdrant/qdrant")
    status, body = cp.route("POST", "/plugins/rag/enable", {"confirm": True})
    assert status == 200, body
    assert "qdrant" in body["apply"]["recreated"]
