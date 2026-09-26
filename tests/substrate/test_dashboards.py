"""The control-plane UI is pluggable data, like agents: a deployment selects a dashboard by id.

There is exactly one shipped dashboard (`dashboard`) and it has no backend service of its own: it
calls `ops-controller`, which serves every route it uses. The `native` SPA and the `v1-parity` +
`ops-api` pair are gone; what those tests actually protected (the GPU reservations, the recreate
guardrails, the in-container probe paths) is asserted here against the one that remains.
"""
from pathlib import Path

import pytest
import yaml

from ordo.control.broker import DockerBackend
from ordo.render.agents import AgentRegistry
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.dashboards import DashboardRegistry
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
AGENTS = AgentRegistry.load(ROOT / "services")
DASHBOARDS = DashboardRegistry.load(ROOT / "services")


def _src(dashboard: str = "dashboard"):
    return Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                             "model": "auto", "plugins": "auto", "dashboard": dashboard})


def _compose(dashboard: str, tmp_path):
    render(_src(dashboard), CATALOG, REGISTRY, agents=AGENTS, dashboards=DASHBOARDS).write(tmp_path)
    return yaml.safe_load((tmp_path / "docker-compose.yml").read_text())


# ── registry basics ────────────────────────────────────────────────────────────
def test_the_shipped_dashboard_is_the_default():
    d = DASHBOARDS.default_dashboard()
    assert d is not None and d.id == "dashboard" and d.default


def test_no_dashboard_declares_a_backend_service():
    """`ops-controller` is the control plane. A dashboard that shipped its own API service was the
    transitional shape while routes were being ported off ops-api; nothing declares one now."""
    for d in DASHBOARDS.dashboards:
        assert not hasattr(d, "backend"), f"{d.id} still declares a companion backend"


def test_unknown_dashboard_falls_back_to_default_with_warning():
    d, notes = DASHBOARDS.resolve("nope")
    assert d is not None and d.id == "dashboard"  # a dashboard is not optional
    assert any("not in the registry" in n for n in notes)


# ── the rendered dashboard service ─────────────────────────────────────────────
def test_render_uses_the_shipped_dashboard_image(tmp_path):
    c = _compose("dashboard", tmp_path)
    assert c["services"]["dashboard"]["image"] == "ordo/dashboard:current"


def test_dashboard_points_at_the_v2_control_plane(tmp_path):
    """The dashboard's FastAPI backend reads OPS_CONTROLLER_URL at runtime. It pointed at ops-api
    while the routes were being ported one slice at a time; slice 4 finished the port, so it now
    points at ops-controller, which is the only control plane."""
    c = _compose("dashboard", tmp_path)
    env = c["services"]["dashboard"]["environment"]
    assert env["OPS_CONTROLLER_URL"] == "http://ops-controller:9000"
    assert c["services"]["dashboard"]["depends_on"] == {"ops-controller": {"condition": "service_started"}}


def test_no_ops_api_service_is_rendered(tmp_path):
    c = _compose("dashboard", tmp_path)
    assert "ops-api" not in c["services"]


def test_the_scheduler_keeps_its_name_and_command(tmp_path):
    """Live clients (Hermes, the ComfyUI gate, the dashboard) all address `ops-controller`."""
    c = _compose("dashboard", tmp_path)
    ctrl = c["services"]["ops-controller"]
    assert ctrl["image"] == "ordo/ops-controller:current"
    assert "serve" in ctrl["command"]


# ── what the retired ops-api env flags protected ───────────────────────────────
def test_whole_stack_mutations_are_not_reachable_from_a_dashboard_button(tmp_path):
    """The migration-triggering root cause must not come back. ops-api gated this with
    OPS_COMPOSE_MUTATIONS_ENABLED=0; the v2 control plane has no whole-stack verb wired to a
    dashboard card at all, and refuses to cycle the services running the request."""
    c = _compose("dashboard", tmp_path)
    assert "OPS_COMPOSE_MUTATIONS_ENABLED" not in c["services"]["ops-controller"].get("environment", {})
    assert DockerBackend.SELF_REFERENTIAL == frozenset({"agent", "ops-controller"})


def test_control_plane_mounts_the_rendered_tree_so_render_and_recreate_share_one_env(tmp_path):
    """A recreate REPLAYS the rendered tree (compose + .env + secrets.env). The control plane must
    mount that tree RW so a model switch's re-render and the recreate replay share one .env."""
    c = _compose("dashboard", tmp_path)
    ctrl = c["services"]["ops-controller"]
    assert "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out:/config" in ctrl["volumes"]


def test_the_control_plane_does_not_mount_the_gguf_weights(tmp_path):
    """It has no route that reads them. The on-disk GGUF list is the DASHBOARD's (routes_console._disk_files),
    served from its own mount; the control plane's /model-config lists CATALOG entries. A mount
    here would exist only to feed code nothing calls."""
    c = _compose("dashboard", tmp_path)
    assert not any("gguf" in v for v in c["services"]["ops-controller"]["volumes"])


def test_control_plane_can_see_comfyui_custom_nodes(tmp_path):
    """/comfyui/install-node-requirements checks for a pack's requirements.txt before running pip
    inside the comfyui container. The nodes live in the comfyui-app volume and nowhere else."""
    c = _compose("dashboard", tmp_path)
    ctrl = c["services"]["ops-controller"]
    assert ctrl["environment"]["COMFYUI_CUSTOM_NODES_DIR"] == "/comfyui-app/ComfyUI/custom_nodes"
    assert any(v.startswith("comfyui-app:") for v in ctrl["volumes"])


# ── the dashboard service's own requirements ───────────────────────────────────
def test_dashboard_mounts_the_gguf_dir_for_the_llm_endpoints(tmp_path):
    c = _compose("dashboard", tmp_path)
    dash = c["services"]["dashboard"]
    assert dash["environment"]["GGUF_MODELS_DIR"] == "/gguf-models"
    assert any(v.endswith(":/gguf-models") for v in dash["volumes"])


def test_dashboard_has_a_healthcheck(tmp_path):
    """The agent gates on `dashboard: service_healthy` (audit G5)."""
    c = _compose("dashboard", tmp_path)
    assert "healthcheck" in c["services"]["dashboard"]


def test_dashboard_reserves_no_gpu(tmp_path):
    """`hardware_stats()`'s GPU widgets read ops-controller `GET /gpus` (ordo/render/gpu_live.py),
    so the dashboard needs no GPU visibility of its own: no reservation, on any vendor."""
    c = _compose("dashboard", tmp_path)
    assert "deploy" not in c["services"]["dashboard"]
    assert DASHBOARDS.default_dashboard().gpu_capabilities == ()


def test_a_dashboard_manifest_declares_gpu_visibility_as_data():
    """The generic mechanism stays: `gpu: <cap>` shorthand or a `gpu_capabilities:` list."""
    from ordo.render.dashboards import Dashboard
    assert Dashboard.from_dict({"id": "x", "gpu": "utility"}).gpu_capabilities == ("utility",)
    assert Dashboard.from_dict({"id": "x", "gpu_capabilities": ["utility"]}).gpu_capabilities == ("utility",)


def test_the_host_base_path_does_not_reach_the_dashboard(tmp_path):
    """`hardware_stats()` calls psutil.disk_usage(BASE_PATH), defaulting to the mounted
    /data/dashboard (dashboard/app.py). The rendered .env carries BASE_PATH=<Windows host path> for
    compose's bind-mount interpolation; in the Linux container that path would make disk_usage raise.
    No service loads .env whole any more and the dashboard does not declare BASE_PATH, so the host
    path cannot reach it and the in-container default applies."""
    c = _compose("dashboard", tmp_path)
    svc = c["services"]["dashboard"]
    assert "env_file" not in svc
    assert "BASE_PATH" not in svc["environment"]


def test_this_deployments_source_selects_the_shipped_dashboard():
    src_path = ROOT / "ordo.yaml"
    if not src_path.exists():
        pytest.skip("operator ordo.yaml not present (gitignored); operator-config guard N/A here")
    assert Source.load(src_path).dashboard == "dashboard"
