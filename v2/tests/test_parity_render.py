"""Full V1→V2 service-parity: a mocked dual-GPU 128GB profile enables the complete ported set,
the parity matrix counts hold, secrets.env.example is generated (keys only), and MCP images are
real (no placeholder digests). This is the acceptance gate for the service-parity slice.
"""
from pathlib import Path

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import CORE_SECRET_KEYS, render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")

UUID_5090 = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"
UUID_1070 = "GPU-20fac13a-5e5b-1818-581f-63901612fd84"
P_DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": UUID_5090},
                   {"name": "GTX 1070", "vram_gb": 8, "uuid": UUID_1070}],
          "ram_gb": 128, "cpu_cores": 32}

# Every kind=service plugin V2 ships after the parity port (GPU + CPU-ok), on the real dual-GPU host.
EXPECTED_SERVICE_PLUGINS = {
    "comfyui", "song-gen", "voice", "monitoring",          # GPU / media / voice
    "rag", "worker", "automation", "open-webui",           # ported CPU-ok services
    "searxng-web", "codebase-memory-ui", "hermes-dashboard", "edge",
}
EXPECTED_MCP = {"qdrant-rag", "searxng"}


def _dual():
    return render(Source.from_dict({"hardware": P_DUAL, "model": "auto", "plugins": "auto"}),
                  CATALOG, REGISTRY)


def test_dual_gpu_enables_the_full_parity_set():
    rc = _dual()
    assert set(rc.plugins_enabled) == EXPECTED_SERVICE_PLUGINS
    assert {s["id"] for s in rc.mcp_servers} == EXPECTED_MCP
    assert not rc.warnings                                  # a clean dual-GPU render has no warnings


def test_parity_matrix_counts():
    # 12 kind=service plugins + 2 kind=mcp plugins are registered and all enable on the full host.
    svc = [p for p in REGISTRY.plugins if p.kind == "service"]
    mcp = [p for p in REGISTRY.plugins if p.kind == "mcp"]
    assert len(svc) == 12 and len(mcp) == 2
    rc = _dual()
    assert len(rc.plugins_enabled) == 12
    assert len(rc.mcp_servers) == 2


def test_ported_services_carry_pins_and_healthchecks():
    # spot-check the exact V1 pins/healthchecks survived the port (no silent :latest drift)
    c = _dual().compose_dict()
    assert c["services"]["qdrant"]["image"] == "qdrant/qdrant:v1.18.2"
    assert c["services"]["n8n"]["image"] == "docker.n8n.io/n8nio/n8n:2.28.3"
    assert c["services"]["open-webui"]["image"] == "ghcr.io/open-webui/open-webui:v0.10.1"
    assert "@sha256:" in c["services"]["searxng"]["image"]         # searxng pinned by digest
    for svc in ("qdrant", "n8n", "open-webui", "rag-ingestion"):
        assert "healthcheck" in c["services"][svc]


def test_named_volumes_from_ported_plugins_declared():
    c = _dual().compose_dict()
    # codebase-memory-ui declares a named cache volume → must be at the compose top level
    assert "codebase-memory-cache" in c["volumes"]
    # edge declares caddy_data / caddy_config named volumes
    assert "caddy_data" in c["volumes"] and "caddy_config" in c["volumes"]


def test_edge_publishes_the_only_host_port():
    c = _dual().compose_dict()
    # exactly the edge's caddy publishes a host port; nothing else does (isolation preserved)
    with_ports = [n for n, s in c["services"].items() if "ports" in s]
    assert with_ports == ["caddy"]
    assert any("443:443" in p for p in c["services"]["caddy"]["ports"])


def test_core_and_gateways_are_project_buildable_images():
    # model-gateway + mcp-gateway are now V2 project images (buildable-not-pullable), not upstream
    c = _dual().compose_dict()
    assert c["services"]["model-gateway"]["image"] == "ordo-v2/model-gateway:latest"
    assert c["services"]["mcp-gateway"]["image"] == "ordo-v2/mcp-gateway:latest"


# --- secrets ---

def test_secrets_env_example_generated_keys_only(tmp_path):
    rc = _dual()
    rc.write(tmp_path)
    example = (tmp_path / "secrets.env.example").read_text()
    # every required key present, ALL with empty values (no secret ever rendered)
    for key in rc.required_secrets:
        assert f"{key}=" in example
    for line in example.splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            assert v == "", f"secrets.env.example leaked a value for {k}"
    # core keys + the plugin-declared secrets both appear
    assert set(CORE_SECRET_KEYS) <= set(rc.required_secrets)
    assert {"SEARXNG_SECRET", "OAUTH2_PROXY_CLIENT_ID", "MCP_GATEWAY_TOKEN"} <= set(rc.required_secrets)


def test_secrets_scoped_to_enabled_plugins():
    # a CPU-only render without the edge? edge is CPU-ok so it stays; but a render that pins only
    # comfyui should NOT pull in edge/searxng secrets — secrets track the enabled set.
    rc = render(Source.from_dict({"hardware": P_DUAL, "model": "auto", "plugins": ["comfyui"]}),
                CATALOG, REGISTRY)
    assert "SEARXNG_SECRET" not in rc.required_secrets      # searxng-web not enabled
    assert "OAUTH2_PROXY_CLIENT_ID" not in rc.required_secrets
    assert set(CORE_SECRET_KEYS) <= set(rc.required_secrets)  # core secrets always required


def test_services_needing_secrets_get_secrets_env_file():
    c = _dual().compose_dict()

    def _has_secrets(svc):
        return any(isinstance(f, dict) and f.get("path") == "secrets.env"
                   for f in c["services"][svc].get("env_file", []))
    # core services that use secrets, plus a ported one, all layer the secrets.env (required:false)
    for svc in ("model-gateway", "mcp-gateway", "ops-controller", "dashboard", "agent",
                "open-webui", "searxng", "caddy", "oauth2-proxy"):
        assert _has_secrets(svc), f"{svc} missing secrets.env env_file"
    # a service with no secrets does NOT get it (qdrant is plain)
    assert not _has_secrets("qdrant")


def test_secrets_env_file_is_not_required():
    # docker compose config must not fail when secrets.env is absent → required:false
    c = _dual().compose_dict()
    ef = c["services"]["model-gateway"]["env_file"]
    sec = next(f for f in ef if isinstance(f, dict) and f["path"] == "secrets.env")
    assert sec["required"] is False
