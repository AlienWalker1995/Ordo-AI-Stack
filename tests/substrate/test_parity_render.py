"""Full V1→V2 service-parity: a mocked dual-GPU 128GB profile enables the complete ported set,
the parity matrix counts hold, secrets.env.example is generated (keys only), and MCP images are
real (no placeholder digests). This is the acceptance gate for the service-parity slice.
"""
from pathlib import Path

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import CORE_SECRET_KEYS, render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
# The site keys the edge and memory-vault plugins require (`requires.site`), so they render.
REQUIRED_SITE = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
                 "CADDY_TAILNET_DOMAIN": "example.ts.net", "MEMORY_VAULT_PATH": "/srv/vault"}

UUID_5090 = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"
UUID_1070 = "GPU-20fac13a-5e5b-1818-581f-63901612fd84"
P_DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": UUID_5090, "compute_cap": "12.0"},
                   {"name": "GTX 1070", "vram_gb": 8, "uuid": UUID_1070, "compute_cap": "6.1"}],
          "ram_gb": 128, "cpu_cores": 32}

# Every kind=service plugin V2 ships after the parity port (GPU + CPU-ok), on the real dual-GPU host.
EXPECTED_SERVICE_PLUGINS = {
    "comfyui", "song-gen", "voice", "monitoring",          # GPU / media / voice
    "rag", "automation", "open-webui",                     # ported CPU-ok services
    "searxng-web", "codebase-memory-ui", "hermes-dashboard", "edge",
    "ltx-trainer",                                        # LoRA trainer (sole trainer since 2026-07-24)
    "obsidian-livesync",                                 # cross-device Obsidian notes sync (CouchDB LiveSync + bridge)
    "llamacpp-cpu",                                      # CPU LLM fallback (needs 24GB RAM, no GPU); dormant behind the cpu-fallback profile
}
# memory-vault is a post-parity add (file-based markdown-memory MCP). codebase-memory / comfyui /
# n8n / orchestration are the RESTORED V1 roster (the V1→V2 migration had silently dropped them);
# these are SERVER ids — the comfyui MCP plugin is id `comfyui-mcp` but renders under server_id
# `comfyui` to preserve Hermes' V1 `comfyui__*` tool namespace.
EXPECTED_MCP = {"qdrant-rag", "searxng", "memory-vault",
                "codebase-memory", "comfyui", "n8n", "orchestration"}


def _dual():
    return render(Source.from_dict({"hardware": P_DUAL, "model": "auto", "plugins": "auto", "site": REQUIRED_SITE}),
                  CATALOG, REGISTRY)


def test_dual_gpu_enables_the_full_parity_set():
    rc = _dual()
    assert set(rc.plugins_enabled) == EXPECTED_SERVICE_PLUGINS
    assert {s["id"] for s in rc.mcp_servers} == EXPECTED_MCP
    assert not rc.warnings                                  # a clean dual-GPU render has no warnings


def test_parity_matrix_counts():
    # 18 kind=service plugins are REGISTERED (11 parity set + ltx-trainer + tailnet-names +
    # obsidian-livesync + obsidian-livesync-funnel + llamacpp-cpu + langfuse + evals) and 7 kind=mcp
    # (qdrant-rag, searxng, memory-vault + the restored codebase-memory / comfyui-mcp / n8n /
    # orchestration). 14 of the service plugins ENABLE on the full host: langfuse, evals, the public
    # Funnel and the tailnet-name sidecars are opt-in (`default: false`), so `plugins: auto`
    # deliberately leaves them out. (The media "worker"
    # plugin was retired, dropping the parity set from 12 to 11; llamacpp-cpu - the CPU LLM
    # fallback - was added post-parity.)
    svc = [p for p in REGISTRY.plugins if p.kind == "service"]
    mcp = [p for p in REGISTRY.plugins if p.kind == "mcp"]
    opt_in = [p.id for p in svc if not p.default]
    assert len(svc) == 18 and len(mcp) == 7
    assert opt_in == ["evals", "langfuse", "obsidian-livesync-funnel", "tailnet-names"]
    rc = _dual()
    assert len(rc.plugins_enabled) == 14
    assert len(rc.mcp_servers) == 7


def test_ported_services_carry_pins_and_healthchecks():
    # spot-check the exact V1 pins/healthchecks survived the port (no silent :latest drift)
    c = _dual().compose_dict()
    assert c["services"]["qdrant"]["image"] == "qdrant/qdrant:v1.19.1"
    assert c["services"]["n8n"]["image"] == "docker.n8n.io/n8nio/n8n:2.38.4"
    assert c["services"]["open-webui"]["image"] == "ghcr.io/open-webui/open-webui:v0.11.3"
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


def test_core_gateway_and_mcp_services_are_project_or_pinned_images():
    c = _dual().compose_dict()
    assert c["services"]["model-gateway"]["image"] == "ordo/model-gateway:current"
    assert "mcp-gateway" not in c["services"]
    for n in ("mcp-orchestration", "mcp-qdrant-rag", "mcp-comfyui", "mcp-codebase-memory",
              "mcp-memory-vault", "mcp-n8n"):
        assert c["services"][n]["image"].startswith("ordo/")
    for n in ("mcp-searxng",):
        assert "@sha256:" in c["services"][n]["image"]


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
    assert {"SEARXNG_SECRET", "OAUTH2_PROXY_CLIENT_ID", "LITELLM_KEY_HERMES"} <= set(rc.required_secrets)
    assert {"LITELLM_SALT_KEY", "LITELLM_DB_PASSWORD"} <= set(CORE_SECRET_KEYS)


def test_throughput_record_token_is_not_a_required_secret():
    """THROUGHPUT_RECORD_TOKEN must NOT be demanded as a required secret: there is no SOPS source
    that can supply it, and the dashboard only enforces it "when set" (the /api/throughput/record
    route is open when the var is empty). Requiring it would list an unfulfillable key in
    secrets.env.example and fail a secrets-completeness preflight for a key nothing can provide."""
    rc = _dual()
    assert "THROUGHPUT_RECORD_TOKEN" not in rc.required_secrets
    assert "THROUGHPUT_RECORD_TOKEN" not in CORE_SECRET_KEYS


def test_secrets_scoped_to_enabled_plugins():
    # a CPU-only render without the edge? edge is CPU-ok so it stays; but a render that pins only
    # comfyui should NOT pull in edge/searxng secrets — secrets track the enabled set.
    rc = render(Source.from_dict({"hardware": P_DUAL, "model": "auto", "plugins": ["comfyui"]}),
                CATALOG, REGISTRY)
    assert "SEARXNG_SECRET" not in rc.required_secrets      # searxng-web not enabled
    assert "OAUTH2_PROXY_CLIENT_ID" not in rc.required_secrets
    assert set(CORE_SECRET_KEYS) <= set(rc.required_secrets)  # core secrets always required


def test_services_get_secret_references_not_the_secrets_file():
    # Per-service scoping is specified in test_secret_scoping.py; here only the parity shape: the
    # services that need secrets carry files or ${KEY} references and none loads secrets.env whole.
    c = _dual().compose_dict()
    for svc in c["services"].values():
        assert all("secrets.env" not in str(f) for f in svc.get("env_file", []))
    assert c["services"]["ops-controller"]["environment"]["OPS_CONTROLLER_TOKEN_FILE"] == "/run/secrets/ops_controller_token"
    proxy = c["services"]["oauth2-proxy"]["environment"]
    assert proxy["OAUTH2_PROXY_COOKIE_SECRET_FILE"] == "/run/secrets/oauth2_proxy_cookie_secret"
    assert proxy["OAUTH2_PROXY_CLIENT_ID"] == "${OAUTH2_PROXY_CLIENT_ID}"   # no file flag; not a secret
    assert "OPS_CONTROLLER_TOKEN" not in (c["services"]["qdrant"].get("environment") or {})


def test_edge_mounts_tracked_config_not_copies():
    # The Caddyfile and SSO email allowlist are TRACKED repo files (tested by
    # tests/test_caddyfile_invariants.py). The edge services must bind-mount them via
    # ${BASE_PATH} so the tracked file is the single source of truth — a `./`-relative
    # mount resolves against the compose project dir (out) and serves a stale COPY
    # (the drift that shipped an old Caddyfile on 2026-07-15).
    #
    # These two are SECURITY-CRITICAL, so they use the fail-loud `${BASE_PATH:?...}` form (not
    # `:-.`): an empty/unset BASE_PATH would otherwise make Docker fabricate an empty dir at the
    # mount → zero-email allowlist → deny-all outage. `:?` rejects an empty value at compose-config
    # time. (Guarded in detail by test_compose.test_edge_security_mounts_fail_loud_on_empty_base_path.)
    c = _dual().compose_dict()
    assert ("${BASE_PATH:?BASE_PATH must be set (non-empty)}/auth/caddy/Caddyfile:/etc/caddy/Caddyfile:ro"
            in c["services"]["caddy"]["volumes"])
    assert (
        "${BASE_PATH:?BASE_PATH must be set (non-empty)}/auth/oauth2-proxy/emails.txt:/etc/oauth2-proxy/emails.txt:ro"
        in c["services"]["oauth2-proxy"]["volumes"]
    )


def test_ops_controller_image_ships_all_render_data():
    # The ops-controller image is the in-place render/serve vehicle. If it lacks any of the
    # renderer's data, renders inside it silently regress (a missing dashboard manifest dropped
    # the v1-parity dashboard from the live compose on 2026-07-15 — resolve() warned, but the
    # compose still came out wrong). The render manifests are now co-located under services/<id>/
    # (plugin.yaml / agent.yaml / dashboard.yaml), so shipping services/ ships every one of them;
    # guard catalog/ and services/ (the two data trees the renderer loads).
    dockerfile = (ROOT / "services" / "ops-controller" / "Dockerfile").read_text(encoding="utf-8")
    for data_dir in ("catalog", "services"):
        assert f"COPY {data_dir} ./{data_dir}" in dockerfile, f"image must ship {data_dir}/"
