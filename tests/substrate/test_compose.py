"""Rendered compose is isolated + correct so it can run without colliding with other projects."""
from pathlib import Path

import yaml

from ordo import compose
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


def test_core_services_present():
    c = compose.render_compose(has_gpu=True, compose_profiles=["media", "voice"])
    for s in compose.core_services() + ["agent"]:
        assert s in c["services"]


def test_isolated_no_port_clashes():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    assert c["name"] == "ordo"
    assert "ordo-net" in c["networks"]
    for name, svc in c["services"].items():
        assert "ports" not in svc, f"{name} publishes a host port (would clash)"
        assert "container_name" not in svc, f"{name} pins a name (would clash)"
        assert svc["networks"] == ["ordo-net"]


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
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    ops = c["services"]["ops-controller"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in ops["volumes"]  # drives the broker
    # but it's launched scoped to the project — the guard can't reach ordo-ai-stack-*
    assert "--project" in ops["command"] and "ordo" in ops["command"]


def test_ops_controller_has_utility_gpu_visibility():
    # The scheduler REPLACES V1's reactive guardian; its whole job is VRAM-fit co-run admission.
    # It detects VRAM by shelling to nvidia-smi INSIDE its container — which the NVIDIA toolkit
    # only injects when the service reserves a GPU with the `utility` capability. Without it the
    # scheduler sees CPU-only (total_vram=0) and drops every GPU plugin. V1's ops-controller has
    # caps=[[utility]]; guard that V2 renders the same read-only visibility.
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
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
    backend = {"name": "ops-api", "image": "ordo/ops-api:latest",
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
    dashboards = DashboardRegistry.load(ROOT / "services")
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto", "dashboard": "v1-parity"})
    c = render(src, CATALOG, REGISTRY, dashboards=dashboards).compose_dict()
    devs = c["services"]["ops-api"]["deploy"]["resources"]["reservations"]["devices"]
    assert any(d.get("capabilities") == ["utility"] for d in devs), \
        "the v1-parity ops-api backend must reserve a GPU with the utility cap (nvidia-smi injection)"


def test_agent_swappable():
    """The agent is pluggable: any id renders as <project>/agent-<id>:latest.

    Uses a deliberately fictional id. The previous fixture named a real (now dead)
    agent, which read as if that one were special-cased — an arbitrary name proves
    the convention is generic, which is the actual contract under test.
    """
    c = compose.render_compose(has_gpu=False, compose_profiles=[], agent="someagent")
    assert "agent-someagent" in c["services"]["agent"]["image"]


def test_llamacpp_image_defaults_to_upstream():
    c = compose.render_compose(has_gpu=True, compose_profiles=[])
    assert c["services"]["llamacpp"]["image"] == (
        "ghcr.io/ggml-org/llama.cpp:server"
        "@sha256:295dc9897fa8a643e4a513fbcaada51d3b8db4b0afa4fda7aeae2386757de58b"
    )


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
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    mg = c["services"]["mcp-gateway"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in mg["volumes"]
    assert "./mcp:/mcp-config" in mg["volumes"]
    assert mg["environment"]["MCP_CONFIG_FILE"] == "/mcp-config/servers.txt"
    assert "healthcheck" in mg


# ── Defect class: restored MCP servers spawn as siblings and read the bind-allowlist + non-secret
#    defaults from the gateway env (the wrapper substitutes PLACEHOLDER_* from the process env).
#    codebase-memory's read-only /c/dev bind is REJECTED unless CODE_ROOT is on the allowlist. ──
def test_mcp_gateway_env_wires_restored_server_placeholders():
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    env = c["services"]["mcp-gateway"]["environment"]
    # codebase-memory: read-only /c/dev bind is only accepted if CODE_ROOT is on the allowlist.
    assert env["MCP_GATEWAY_DOCKER_BIND_ALLOWED_PATHS"] == "${CODE_ROOT:-/c/dev}"
    assert env["CODE_ROOT"] == "${CODE_ROOT:-/c/dev}"
    # comfyui MCP's non-secret default checkpoint (safe to interpolate from .env-space).
    assert env["COMFY_MCP_DEFAULT_MODEL"] == "${COMFY_MCP_DEFAULT_MODEL:-flux1-schnell-fp8.safetensors}"
    # memory-vault must remain wired (no regression).
    assert env["MEMORY_VAULT_PATH"] == "${MEMORY_VAULT_PATH:-}"


def test_mcp_gateway_does_not_shadow_env_file_secrets():
    # OPS_CONTROLLER_TOKEN / N8N_API_KEY arrive via the secrets.env env_file. They must NOT be
    # re-declared in `environment:` — a `${VAR:-}` there interpolates from .env/host (empty) and
    # shadows the env_file value to empty, breaking the spawned MCP servers' auth.
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    env = c["services"]["mcp-gateway"]["environment"]
    for k in ("OPS_CONTROLLER_TOKEN", "N8N_API_KEY"):
        assert k not in env, f"{k} must come from secrets.env env_file, not the environment block"


def test_model_without_backend_image_keeps_default(tmp_path):
    # a small GPU best-fits a stock model (no backend_image) -> upstream image, no LLAMACPP_IMAGE
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 8}], "ram_gb": 32},
                            "model": "auto", "plugins": "auto"})
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.backend_image is None
    assert "LLAMACPP_IMAGE" not in rc.env
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["llamacpp"]["image"] == (
        "ghcr.io/ggml-org/llama.cpp:server"
        "@sha256:295dc9897fa8a643e4a513fbcaada51d3b8db4b0afa4fda7aeae2386757de58b"
    )


def test_edge_security_mounts_fail_loud_on_empty_base_path():
    """The oauth2-proxy allowlist (emails.txt) and the Caddyfile are SECURITY-CRITICAL host
    binds. They MUST use the `${BASE_PATH:?...}` fail-loud form, not `${BASE_PATH:-.}`: an
    empty/unset BASE_PATH with `:-.` makes Docker fabricate an empty directory at the mount
    → zero-email allowlist → deny-all outage (this happened). `:?` makes `docker compose config`
    reject an empty value instead. Matches the CADDY_BIND `:?` precedent in the same plugin."""
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["edge"]})
    c = render(src, CATALOG, REGISTRY).compose_dict()

    emails_mounts = [v for v in c["services"]["oauth2-proxy"]["volumes"] if "emails.txt" in v]
    assert emails_mounts, "oauth2-proxy has no emails.txt allowlist mount"
    for v in emails_mounts:
        assert "${BASE_PATH:?" in v, f"emails.txt mount not fail-loud on empty BASE_PATH: {v}"
        assert "${BASE_PATH:-" not in v, f"emails.txt mount still uses the clobber-prone :- form: {v}"

    caddyfile_mounts = [v for v in c["services"]["caddy"]["volumes"] if "/Caddyfile" in v]
    assert caddyfile_mounts, "caddy has no Caddyfile mount"
    for v in caddyfile_mounts:
        assert "${BASE_PATH:?" in v, f"Caddyfile mount not fail-loud on empty BASE_PATH: {v}"
        assert "${BASE_PATH:-" not in v, f"Caddyfile mount still uses the clobber-prone :- form: {v}"


def test_gpu_exporter_healthcheck_overrides_baked_wget():
    """The nvidia_gpu_exporter image BAKES a wget HEALTHCHECK but ships no wget, so the
    inherited check can never pass → permanently unhealthy. (The earlier fix removed the
    declared check believing the image distroless — wrong on both counts: it is debian-full
    with bash, and omission INHERITS the baked check rather than disabling it.) The rendered
    service must therefore declare an override the image can actually run: bash /dev/tcp,
    never wget."""
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["monitoring"]})
    c = render(src, CATALOG, REGISTRY).compose_dict()
    hc = c["services"]["gpu-exporter"].get("healthcheck")
    assert hc, "gpu-exporter must override the image's baked (broken) wget healthcheck"
    test_cmd = " ".join(hc["test"])
    assert "wget" not in test_cmd, "wget does not exist in the image — probe can never pass"
    assert "/dev/tcp/" in test_cmd and "bash" in test_cmd, (
        "probe must use bash /dev/tcp — the only connect tool the image ships")


def test_ops_controller_serve_out_matches_deployed_layout():
    # The live deployment mounts the dir HOLDING ordo.yaml AND the rendered outputs as /config
    # (compose project dir = out). serve's --out must therefore be /config itself: writing to
    # /config/out re-renders into a nested dir NOTHING consumes — a model switch would silently
    # never reach the live .env/compose (found live 2026-07-15).
    c = compose.render_compose(has_gpu=True, compose_profiles=[], project="ordo")
    cmd = c["services"]["ops-controller"]["command"]
    assert cmd[cmd.index("--out") + 1] == "/config"
    assert "./:/config" in c["services"]["ops-controller"]["volumes"]


# ── netns members must not outlive the namespace they join ──────────────────────
# Caddy / oauth2-proxy / Tailscale are OPTIONAL layers an operator may never enable.
# A `network_mode: service:X` whose X is not rendered is NOT a degraded mode — compose
# cannot start that container at all. Every service joining caddy's namespace (the
# tailnet-name sidecars, hermes-dashboard) must therefore be gated by `depends_on: [edge]`
# at PLUGIN level so the dep gate drops it when the edge is off.
#
# These use the full render() path on purpose: compose.render_compose() emits only the
# core services, so a netns assertion written against it inspects an empty set and can
# never fail.

def _render_with_plugins(plugins, tmp_path):
    src = Source.from_dict({
        "hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
        "model": "auto", "agent": "hermes", "plugins": list(plugins),
    })
    render(src, CATALOG, REGISTRY).write(tmp_path)
    return yaml.safe_load((tmp_path / "docker-compose.yml").read_text())


def test_no_service_joins_a_namespace_that_is_not_rendered(tmp_path):
    """Across plugin combinations, every `network_mode: service:X` resolves to a rendered X."""
    combos = [
        [],
        ["edge"],
        ["edge", "hermes-dashboard"],
        ["hermes-dashboard"],            # the regression: SPA without the edge
        ["edge", "tailnet-names"],
        ["tailnet-names"],               # sidecars without the edge
        ["open-webui", "automation"],
    ]
    for i, plugins in enumerate(combos):
        c = _render_with_plugins(plugins, tmp_path / f"r{i}")
        for name, svc in c["services"].items():
            mode = str(svc.get("network_mode", ""))
            if mode.startswith("service:"):
                target = mode.split("service:", 1)[1]
                assert target in c["services"], (
                    f"{name} joins '{target}' netns but '{target}' is not rendered for "
                    f"plugins={plugins} — gate its plugin with depends_on")


def test_hermes_dashboard_is_dropped_when_the_edge_is_disabled(tmp_path):
    """Enabling the Hermes SPA without the edge yields NO hermes-dashboard service rather
    than one wired to a caddy that does not exist. It publishes no host port and binds
    loopback inside caddy's namespace, so without the edge it is both unreachable and
    unstartable — dropping it is the honest outcome."""
    c = _render_with_plugins(["hermes-dashboard"], tmp_path)
    assert "hermes-dashboard" not in c["services"]
    assert "service:caddy" not in yaml.safe_dump(c)


def test_hermes_dashboard_renders_in_caddy_netns_when_the_edge_is_on(tmp_path):
    """The positive case, so the test above cannot be satisfied by the plugin simply
    never rendering: with the edge enabled it IS present, in caddy's namespace, on loopback."""
    c = _render_with_plugins(["edge", "hermes-dashboard"], tmp_path)
    hd = c["services"]["hermes-dashboard"]
    assert hd["network_mode"] == "service:caddy"
    assert "caddy" in c["services"]
    assert "127.0.0.1" in hd["command"]
    assert "networks" not in hd, "compose forbids networks: alongside network_mode:"


def test_gguf_models_on_named_volume():
    # GGUF weights are served from the models-gguf named volume (ext4 inside the
    # Docker VM), never a ${BASE_PATH} 9p bind: heavy sequential reads wedge the
    # 9p client in D-state (third casualty 2026-08-07 — after the Hermes brain
    # and the ComfyUI app tree). dashboard mounts RW (pull target lands where the
    # backends read); everything else RO.
    from ordo.dashboards import DashboardRegistry
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": ["llamacpp-cpu", "rag"],
                            "dashboard": "v1-parity"})
    c = render(src, CATALOG, REGISTRY,
               dashboards=DashboardRegistry.load(ROOT / "services")).compose_dict()
    assert "models-gguf" in c["volumes"]
    expected = {
        "llamacpp": "models-gguf:/models:ro",
        "llamacpp-cpu": "models-gguf:/models:ro",
        "llamacpp-embed": "models-gguf:/models:ro",
        "dashboard": "models-gguf:/gguf-models",
        "ops-api": "models-gguf:/gguf-models:ro",
    }
    for name, mount in expected.items():
        vols = c["services"][name]["volumes"]
        assert mount in vols, f"{name} missing {mount}"
        assert not any("models/gguf" in v for v in vols), f"{name} still binds models/gguf over 9p"
    # dashboard must stay RW — an :ro flip would silently break the pull-UI landing path
    assert "models-gguf:/gguf-models:ro" not in c["services"]["dashboard"]["volumes"]


def test_db_state_on_named_volumes_never_9p():
    # Stage-1 rule (2026-08-07 architect review): mounts are either small read-once
    # CONFIG binds or STATE — and state never rides the Docker Desktop 9p bridge,
    # which wedges on metadata ops (p9_client_rpc D-state; casualties: hermes brain,
    # comfyui app, gguf models, a live rag-ingestion worker). Databases fsync/mmap
    # constantly, so their data dirs must be named volumes. Deliberate 9p exceptions:
    # drop zones the operator reaches from Windows (data/rag-input, data/n8n-files,
    # /c/dev mirror).
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto",
                            "plugins": ["rag", "automation", "open-webui", "obsidian-livesync"]})
    c = render(src, CATALOG, REGISTRY).compose_dict()
    expected = {
        "qdrant": "qdrant-data:/qdrant/storage",
        "couchdb": "couchdb-data:/opt/couchdb/data",
        "n8n": "n8n-data:/home/node/.n8n",
        "open-webui": "open-webui-data:/app/backend/data",
    }
    for name, mount in expected.items():
        vols = c["services"][name]["volumes"]
        assert mount in vols, f"{name} missing {mount}"
        vol_name = mount.split(":", 1)[0]
        assert vol_name in c["volumes"], f"{vol_name} not declared top-level"
        bad = [v for v in vols if "DATA_PATH" in v and mount.split(":", 1)[1].split(":")[0] in v]
        assert not bad, f"{name} still binds its state dir over 9p: {bad}"
