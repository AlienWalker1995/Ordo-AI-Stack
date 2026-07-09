"""Rendered compose is isolated + correct so it can run beside the live stack."""
from pathlib import Path

import yaml

from ordo import compose
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")


def test_core_services_present():
    c = compose.render_compose(has_gpu=True, compose_profiles=["media", "voice"])
    for s in compose.core_services() + ["agent"]:
        assert s in c["services"]


def test_isolated_no_port_clashes():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    assert c["name"] == "ordo-v2"
    assert "ordo-v2-net" in c["networks"]
    for name, svc in c["services"].items():
        assert "ports" not in svc, f"{name} publishes a host port (would clash)"
        assert "container_name" not in svc, f"{name} pins a name (would clash)"
        assert svc["networks"] == ["ordo-v2-net"]


def test_gpu_reservation_gated_by_hardware():
    with_gpu = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert "deploy" in with_gpu["services"]["llamacpp"]
    no_gpu = compose.render_compose(has_gpu=False, compose_profiles=[])
    assert "deploy" not in no_gpu["services"]["llamacpp"]


def test_plugin_services_behind_profiles():
    # data-driven: render() resolves the enabled plugins → compose builds their services.
    # On a single 32GB GPU, comfyui is enabled (media) and behind its profile; voice is off.
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    c = render(src, CATALOG, REGISTRY).compose_dict()
    assert c["services"]["comfyui"]["profiles"] == ["media"]
    assert "stt" not in c["services"] and "tts" not in c["services"]  # voice needs a 2nd GPU
    # no plugins requested → only core + agent, no plugin services
    c2 = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert "comfyui" not in c2["services"]


def test_llamacpp_emits_metrics():
    # render always emits --metrics so the monitoring plugin's prometheus can scrape :8080
    c = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert c["services"]["llamacpp"]["command"] == ["--metrics"]


def test_monitoring_named_volumes_declared():
    # prometheus-data / grafana-data are named volumes → must appear at the top level
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["monitoring"]})
    c = render(src, CATALOG, REGISTRY).compose_dict()
    assert c["services"]["grafana"]["profiles"] == ["monitoring"]
    assert "prometheus-data" in c["volumes"] and "grafana-data" in c["volumes"]
    # gpu-exporter keeps the driver-581.80 field-pin command
    assert any("query-field-names" in a for a in c["services"]["gpu-exporter"]["command"])


def test_ops_controller_has_scoped_socket():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    ops = c["services"]["ops-controller"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in ops["volumes"]  # drives the broker
    # but it's launched scoped to the project — the guard can't reach ordo-ai-stack-*
    assert "--project" in ops["command"] and "ordo-v2" in ops["command"]


def test_ops_controller_has_utility_gpu_visibility():
    # The scheduler REPLACES V1's reactive guardian; its whole job is VRAM-fit co-run admission.
    # It detects VRAM by shelling to nvidia-smi INSIDE its container — which the NVIDIA toolkit
    # only injects when the service reserves a GPU with the `utility` capability. Without it the
    # scheduler sees CPU-only (total_vram=0) and drops every GPU plugin. V1's ops-controller has
    # caps=[[utility]]; guard that V2 renders the same read-only visibility.
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    ops = c["services"]["ops-controller"]
    devs = ops["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["utility"] for d in devs), \
        "ops-controller must reserve a GPU with the `utility` cap so nvidia-smi works for the scheduler"


def test_plain_gpu_service_reserves_gpu_capability():
    # A regular compute GPU service (gpu=True) must reserve the `gpu` capability — the utility
    # refactor must NOT change that (llamacpp/plugins get compute, not read-only visibility).
    c = compose.render_compose(has_gpu=True, compose_profiles=[])
    devs = c["services"]["llamacpp"]["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["gpu"] for d in devs), \
        "a plain gpu:true service must reserve the compute `gpu` capability, not `utility`"


def test_dashboard_backend_renders_utility_gpu_reservation():
    # A dashboard backend that declares `gpu_capabilities: [utility]` must render an all-GPU
    # (count: all) reservation with the utility cap — the fix for "No GPUs returned from registry".
    backend = {"name": "ops-api", "image": "ordo-v2/ops-api:latest",
               "gpu_capabilities": ["utility"]}
    c = compose.render_compose(has_gpu=True, compose_profiles=[],
                               dashboard={"backend": backend})
    devs = c["services"]["ops-api"]["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["utility"] and d.get("count") == "all" for d in devs), \
        "an ops-api backend with gpu_capabilities:[utility] must reserve all GPUs with the utility cap"


def test_dashboard_backend_without_gpu_has_no_reservation():
    # A backend that declares no GPU capabilities gets no reservation (unchanged behaviour).
    backend = {"name": "some-api", "image": "x:latest"}
    c = compose.render_compose(has_gpu=True, compose_profiles=[],
                               dashboard={"backend": backend})
    assert "deploy" not in c["services"]["some-api"]


def test_v1_parity_ops_api_backend_has_utility_gpu(tmp_path):
    # End-to-end through the real v1-parity manifest + render: the ops-api service the operator's
    # dashboard depends on must carry the utility GPU reservation, else its GPU widgets go blank.
    from ordo.dashboards import DashboardRegistry
    dashboards = DashboardRegistry.load(ROOT / "dashboards")
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto", "dashboard": "v1-parity"})
    c = render(src, CATALOG, REGISTRY, dashboards=dashboards).compose_dict()
    devs = c["services"]["ops-api"]["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["utility"] for d in devs), \
        "the v1-parity ops-api backend must reserve a GPU with the utility cap (nvidia-smi injection)"


def test_agent_swappable():
    c = compose.render_compose(has_gpu=False, compose_profiles=[], agent="openclaw")
    assert "agent-openclaw" in c["services"]["agent"]["image"]


def test_llamacpp_image_defaults_to_upstream():
    c = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert c["services"]["llamacpp"]["image"] == "ghcr.io/ggml-org/llama.cpp:server"


def test_llamacpp_image_override():
    patched = "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470"
    c = compose.render_compose(has_gpu=True, compose_profiles=[], llamacpp_image=patched)
    assert c["services"]["llamacpp"]["image"] == patched


def test_render_writes_runnable_compose(tmp_path):
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    render(src, CATALOG, REGISTRY).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert "llamacpp" in c["services"] and "agent" in c["services"]
    assert c["services"]["comfyui"]["profiles"] == ["media"]   # media enabled on 5090
    assert "deploy" in c["services"]["llamacpp"]               # GPU reserved


def test_backend_image_flows_from_catalog_to_compose_and_env(tmp_path):
    # the 5090 best-fits huihui-qwen3.6-27b-q6, whose catalog entry pins the patched build
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.backend_image == "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470"
    assert rc.env["LLAMACPP_IMAGE"] == rc.model.backend_image
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["llamacpp"]["image"] == rc.model.backend_image


def _dual_gpu_src(plugins="auto"):
    # a machine matching the operator's box: 5090 primary + 1070 secondary, each with a real uuid.
    return Source.from_dict({"hardware": {"gpus": [
        {"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-PRIMARY-uuid"},
        {"name": "GTX 1070", "vram_gb": 8, "uuid": "GPU-SECONDARY-uuid"}],
        "ram_gb": 128}, "model": "auto", "plugins": plugins})


# ── Defect class: PRIMARY GPU pin (compute services must be pinned to the primary card by uuid, not
#    `count: all`, or on a dual-GPU WSL2 box they leak onto the 1070 — a live-only failure). ──
def test_llamacpp_pinned_to_primary_gpu_uuid():
    c = render(_dual_gpu_src(plugins=[]), CATALOG, REGISTRY).compose_dict()
    lc = c["services"]["llamacpp"]
    devs = lc["deploy"]["resources"]["reservations"]["devices"][0]
    assert devs["device_ids"] == ["GPU-PRIMARY-uuid"]        # pinned by uuid, not count:all
    assert lc["environment"]["CUDA_VISIBLE_DEVICES"] == "GPU-PRIMARY-uuid"   # the WSL2-honored layer
    assert lc["environment"]["NVIDIA_VISIBLE_DEVICES"] == "GPU-PRIMARY-uuid"


def test_comfyui_and_embed_pinned_to_primary_gpu_uuid():
    c = render(_dual_gpu_src(plugins=["comfyui", "rag"]), CATALOG, REGISTRY).compose_dict()
    for name in ("comfyui", "llamacpp-embed"):
        svc = c["services"][name]
        assert svc["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"] == \
            ["GPU-PRIMARY-uuid"], f"{name} not pinned to primary uuid"
        assert svc["environment"]["CUDA_VISIBLE_DEVICES"] == "GPU-PRIMARY-uuid"


def test_voice_pinned_to_secondary_gpu_uuid():
    c = render(_dual_gpu_src(plugins=["voice"]), CATALOG, REGISTRY).compose_dict()
    for name in ("stt", "tts"):
        svc = c["services"][name]
        assert svc["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"] == \
            ["GPU-SECONDARY-uuid"], f"{name} not pinned to secondary (1070) uuid"
        assert svc["environment"]["CUDA_VISIBLE_DEVICES"] == "GPU-SECONDARY-uuid"


def test_gpu_pin_falls_back_when_no_uuid():
    # a single mock GPU with no uuid (CI) must still render a valid reservation, not crash.
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["comfyui"]})
    c = render(src, CATALOG, REGISTRY).compose_dict()
    assert "deploy" in c["services"]["comfyui"]  # falls back to the all-GPU reservation shape


# ── Defect class: depends_on health CONDITIONS (V1 gates the agent on service_healthy; a plain list
#    lets it start while the gateways are still warming → 5xx storm). ──
def test_agent_depends_on_health_conditions():
    c = render(_dual_gpu_src(plugins=[]), CATALOG, REGISTRY).compose_dict()
    dep = c["services"]["agent"]["depends_on"]
    assert dep["model-gateway"] == {"condition": "service_healthy"}
    assert dep["mcp-gateway"] == {"condition": "service_healthy"}
    assert dep["dashboard"] == {"condition": "service_healthy"}
    assert dep["ops-controller"] == {"condition": "service_started"}


# ── Defect class: mcp-gateway runtime wiring (spawns MCP servers as containers → needs docker.sock;
#    reads the rendered catalog from a mounted config dir; empty catalog = agent has no tools). ──
def test_mcp_gateway_has_socket_config_and_healthcheck():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo-v2")
    mg = c["services"]["mcp-gateway"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in mg["volumes"]
    assert "./mcp:/mcp-config" in mg["volumes"]
    assert mg["environment"]["MCP_CONFIG_FILE"] == "/mcp-config/servers.txt"
    assert "healthcheck" in mg


def test_model_without_backend_image_keeps_default(tmp_path):
    # a small GPU best-fits a stock model (no backend_image) -> upstream image, no LLAMACPP_IMAGE
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 8}], "ram_gb": 32},
                            "model": "auto", "plugins": "auto"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.backend_image is None
    assert "LLAMACPP_IMAGE" not in rc.env
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["llamacpp"]["image"] == "ghcr.io/ggml-org/llama.cpp:server"
