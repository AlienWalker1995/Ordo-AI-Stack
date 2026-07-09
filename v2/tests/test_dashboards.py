"""The control-plane UI is pluggable data (like agents): v2-native is the default; a deployment
can select the V1-parity dashboard + its ops-api backend WITHOUT patching the substrate."""
from pathlib import Path

import yaml

from ordo.agents import AgentRegistry
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.dashboards import DashboardRegistry
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
AGENTS = AgentRegistry.load(ROOT / "agents")
DASHBOARDS = DashboardRegistry.load(ROOT / "dashboards")


def _src(dashboard: str = "v2-native"):
    return Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                             "model": "auto", "plugins": "auto", "dashboard": dashboard})


def _compose(dashboard: str, tmp_path):
    render(_src(dashboard), CATALOG, REGISTRY, agents=AGENTS, dashboards=DASHBOARDS).write(tmp_path)
    return yaml.safe_load((tmp_path / "docker-compose.yml").read_text())


# ── registry basics ────────────────────────────────────────────────────────────
def test_v2_native_is_the_default():
    d = DASHBOARDS.default_dashboard()
    assert d is not None and d.id == "v2-native" and d.default


def test_v2_native_image_convention():
    d = DASHBOARDS.get("v2-native")
    assert d.image == "" and d.image_for("ordo-v2") == "ordo-v2/dashboard:latest"


def test_v1_parity_pins_the_v1_image_and_backend():
    d = DASHBOARDS.get("v1-parity")
    assert d.image_for("ordo-v2") == "ordo-v2/dashboard-v1:latest"
    assert d.backend and d.backend.name == "ops-api"
    assert d.backend.image_for("ordo-v2") == "ordo-v2/ops-api:latest"


def test_unknown_dashboard_falls_back_to_default_with_warning():
    d, notes = DASHBOARDS.resolve("nope")
    assert d is not None and d.id == "v2-native"  # falls back to default (dashboard is not optional)
    assert any("not in the registry" in n for n in notes)


# ── default render (v2-native) stays exactly as before ──────────────────────────
def test_default_render_uses_v2_native_spa(tmp_path):
    c = _compose("v2-native", tmp_path)
    dash = c["services"]["dashboard"]
    assert dash["image"] == "ordo-v2/dashboard:latest"
    assert "ops-api" not in c["services"]  # no separate backend for the default
    # keeps the /api/health healthcheck so the agent's `dashboard: service_healthy` gate holds
    assert "healthcheck" in dash


def test_v2_native_dashboard_still_gates_on_scheduler(tmp_path):
    c = _compose("v2-native", tmp_path)
    dep = c["services"]["dashboard"].get("depends_on", {})
    assert "ops-controller" in dep


# ── v1-parity render: reinstates the V1 dashboard + ops-api backend ─────────────
def test_v1_parity_swaps_the_dashboard_image(tmp_path):
    c = _compose("v1-parity", tmp_path)
    assert c["services"]["dashboard"]["image"] == "ordo-v2/dashboard-v1:latest"


def test_v1_parity_dashboard_points_at_ops_api_not_scheduler(tmp_path):
    # the whole naming resolution: the V1 frontend is same-origin; its FastAPI backend reads
    # OPS_CONTROLLER_URL at runtime, so pointing it at ops-api keeps V2's scheduler named
    # `ops-controller` collision-free (no dashboard rebuild).
    c = _compose("v1-parity", tmp_path)
    env = c["services"]["dashboard"]["environment"]
    assert env["OPS_CONTROLLER_URL"] == "http://ops-api:9000"
    assert c["services"]["dashboard"]["depends_on"] == {"ops-api": {"condition": "service_started"}}


def test_v1_parity_renders_ops_api_backend_service(tmp_path):
    c = _compose("v1-parity", tmp_path)
    assert "ops-api" in c["services"]
    ops = c["services"]["ops-api"]
    assert ops["image"] == "ordo-v2/ops-api:latest"
    # socket for SDK start/stop (guard-scoped) + the registry/audit data mount
    assert any(v.startswith("/var/run/docker.sock") for v in ops["volumes"])
    assert ops["group_add"] == ["0"]  # Docker Desktop root:root socket access


def test_v2_scheduler_service_is_untouched_by_dashboard_choice(tmp_path):
    # V2's `ordo serve` scheduler stays named `ops-controller` and keeps the `ordo serve` command
    # regardless of the dashboard selection — it is the GPU authority.
    c = _compose("v1-parity", tmp_path)
    ctrl = c["services"]["ops-controller"]
    assert ctrl["image"] == "ordo-v2/ops-controller:latest"
    assert "serve" in ctrl["command"]


def test_ops_api_guardian_and_mutations_disabled(tmp_path):
    # the migration-triggering root cause must NOT come back: the reactive guardian + watchdogs are
    # explicitly off, and the WHOLE-STACK compose-mutation endpoints are disabled (scheduler owns them).
    c = _compose("v1-parity", tmp_path)
    env = c["services"]["ops-api"]["environment"]
    assert env["COMFYUI_SERIALIZE_LLAMACPP"] == "0"
    assert env["OPS_VRAM_PRESSURE_GB"] == "0"
    assert env["OPS_HERMES_WATCHDOG_ENABLED"] == "0"
    assert env["OPS_COMPOSE_MUTATIONS_ENABLED"] == "0"  # whole-stack /compose/* STAYS off
    # SDK container actions must be scoped to the ordo-v2 project (never the stopped V1 stack)
    assert env["COMPOSE_PROJECT"] == "ordo-v2"


def test_ops_api_enables_safe_per_service_recreate(tmp_path):
    # The dashboard's REAL buttons (Model Control → llamacpp, default-model → open-webui) proxy to
    # per-service recreate — that narrow path MUST be enabled here (whole-stack mutations stay off).
    c = _compose("v1-parity", tmp_path)
    env = c["services"]["ops-api"]["environment"]
    assert env["OPS_SERVICE_RECREATE_ENABLED"] == "1"     # per-service recreate ON
    assert env["OPS_COMPOSE_MUTATIONS_ENABLED"] == "0"    # but whole-stack still OFF


def test_ops_api_mounts_rendered_out_dir_for_recreate(tmp_path):
    # A safe recreate REPLAYS the existing rendered out/ tree (compose + .env + secrets.env). ops-api
    # must bind-mount that tree RW at the COMPOSE_PROJECT_DIR so /env/set writes and the recreate
    # replay share one .env, and both env files are present for --env-file interpolation.
    c = _compose("v1-parity", tmp_path)
    ops = c["services"]["ops-api"]
    assert "./:/workspace:rw" in ops["volumes"]           # the rendered out/ dir, RW
    assert ops["environment"]["COMPOSE_PROJECT_DIR"] == "/workspace"
    assert ops["environment"]["OPS_ENV_PATH"] == "/workspace/.env"
    # gguf listing dir must NOT nest under the RW out/ mount (avoids mount-ordering fragility)
    assert ops["environment"]["LLAMACPP_MODELS_DIR"] == "/gguf-models"
    assert any(v.endswith(":/gguf-models:ro") for v in ops["volumes"])


def test_v1_parity_dashboard_mounts_gguf_dir_for_llm_endpoints(tmp_path):
    # /api/llm/* lists on-disk GGUFs from GGUF_MODELS_DIR — the dir must be bind-mounted.
    c = _compose("v1-parity", tmp_path)
    dash = c["services"]["dashboard"]
    assert dash["environment"]["GGUF_MODELS_DIR"] == "/gguf-models"
    assert any(v.endswith(":/gguf-models") for v in dash["volumes"])


def test_v1_parity_dashboard_and_ops_api_have_healthchecks(tmp_path):
    # dashboard is `service_healthy`-gated by the agent (audit G5) -> both need a healthcheck.
    c = _compose("v1-parity", tmp_path)
    assert "healthcheck" in c["services"]["dashboard"]
    assert "healthcheck" in c["services"]["ops-api"]


def test_v1_parity_dashboard_reserves_utility_gpu(tmp_path):
    # `/api/hardware`'s GPU widgets shell to nvidia-smi + gpu_stats.list_gpus, which the NVIDIA
    # runtime only injects with a `utility` GPU reservation on the dashboard SERVICE itself.
    # Without it the hw-stat bar's GPU widgets go blank (gpu:null + gpus:[]). Guard the reservation.
    c = _compose("v1-parity", tmp_path)
    devs = c["services"]["dashboard"]["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["utility"] and d.get("count") == "all" for d in devs), \
        "the v1-parity dashboard must reserve all GPUs with the utility cap so nvidia-smi works for /api/hardware"


def test_v1_parity_dashboard_pins_in_container_disk_probe_path(tmp_path):
    # `/api/hardware` calls psutil.disk_usage(BASE_PATH) for the DISK widget. The shared .env carries
    # BASE_PATH=<Windows host path> (needed for compose ${BASE_PATH} interpolation) and env_file leaks
    # it into the Linux container, where disk_usage("C:/...") raises -> disk_*:null. A per-service
    # `environment:` value beats env_file: pin the in-container probe to the always-mounted mount.
    c = _compose("v1-parity", tmp_path)
    assert c["services"]["dashboard"]["environment"]["BASE_PATH"] == "/data/dashboard", \
        "dashboard BASE_PATH must be overridden to an in-container path or /api/hardware disk stats go null"


def test_v2_native_dashboard_reserves_no_gpu(tmp_path):
    # The default SPA declares no GPU visibility -> no reservation (unchanged behaviour).
    c = _compose("v2-native", tmp_path)
    assert "deploy" not in c["services"]["dashboard"]


def test_this_deployments_source_selects_v1_parity():
    # the operator's ordo.yaml pins the reinstated dashboard (regression guard for the reinstatement).
    src = Source.load(ROOT / "ordo.yaml")
    assert src.dashboard == "v1-parity"
