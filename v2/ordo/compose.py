"""Render an ISOLATED docker-compose for the v2 stack from the resolved config.

This is what makes 'start the new stack beside the old one' safe:
  - a dedicated project name + network (no collision with the running stack),
  - NO host port publishes on core services (reached via the dashboard/agent, per the deployment
    model) so nothing fights the current stack's ports,
  - GPU reservations only when a GPU is present,
  - core services read the rendered .env (single source → no drift),
  - plugin services appear only behind their compose profile (media/voice).

The images/build contexts are the substrate's own; this renders the SHAPE and wiring. The
process broker (next slice) starts/stops these against the scheduler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .plugins import Plugin, PluginService

# The mandatory 6-service core (from the architecture decisions). Caddy/oauth2-proxy is an
# OPTIONAL remote-access plugin, so it's not here — a local floor install is localhost-only.
_CORE = ["llamacpp", "model-gateway", "mcp-gateway", "ops-controller", "dashboard"]

# --metrics turns on llama-server's native Prometheus endpoint at /metrics:8080 (token rates,
# queue depth). Always-on — it's cheap, and the monitoring plugin's prometheus scrapes it.
LLAMACPP_METRICS_ARG = "--metrics"

_GPU_RESERVATION = {
    "deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]}}}
}


def _gpu_pinned_reservation(uuid: str) -> dict[str, Any]:
    """Reserve exactly one card by uuid (device_ids). Paired with CUDA_VISIBLE_DEVICES —
    both layers are required on Docker Desktop/WSL2 where device_ids alone is a no-op."""
    return {"deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "device_ids": [uuid], "capabilities": ["gpu"]}]}}}}


def _pin_env(uuid: str) -> dict[str, str]:
    """The CUDA_VISIBLE_DEVICES / NVIDIA_VISIBLE_DEVICES pair — the ONLY thing that actually
    isolates a process to one card under Docker Desktop/WSL2 (device_ids alone is a no-op).
    Mirrors V1's overrides/gpu-assignments.yml for every GPU service (primary AND secondary)."""
    return {"CUDA_VISIBLE_DEVICES": uuid, "NVIDIA_VISIBLE_DEVICES": uuid}


def _depends_on(peers: dict[str, str] | list[str] | None) -> Any:
    """Render depends_on. A plain list -> emitted as-is (start-ordering only). A dict of
    {peer: condition} -> the long form `{peer: {condition: <cond>}}` so V1's service_healthy
    gates are mirrored (the agent must not start until the gateways are HEALTHY, not just up)."""
    if not peers:
        return None
    if isinstance(peers, dict):
        return {p: {"condition": c} for p, c in peers.items()}
    return list(peers)


# Operator-managed secrets live here (SOPS-decrypted / hand-filled), NEVER in the rendered .env.
# Services that need secrets read it as a SECOND env_file layered over the derived .env.
SECRETS_ENV_FILE = "secrets.env"


def _env_files(env_file: str | None, secrets: bool) -> list:
    # secrets.env is operator-managed and may be absent at render/config time (it holds no derived
    # values), so it's declared `required: false` — `docker compose config` must not fail when the
    # operator hasn't filled it yet. `ordo render` emits secrets.env.example listing the keys.
    files: list = [env_file] if env_file else []
    if secrets:
        files.append({"path": SECRETS_ENV_FILE, "required": False})
    return files


def _svc(image: str, *, net: str, env_file: str | None = None, gpu: bool = False,
         profiles: list[str] | None = None, depends: list[str] | None = None,
         secrets: bool = False) -> dict[str, Any]:
    s: dict[str, Any] = {"image": image, "restart": "unless-stopped", "networks": [net]}
    files = _env_files(env_file, secrets)
    if files:
        s["env_file"] = files
    if profiles:
        s["profiles"] = profiles
    if depends:
        s["depends_on"] = depends
    if gpu:
        s.update(_GPU_RESERVATION)
    return s


def _ops_controller(project: str, net: str, env_file: str) -> dict[str, Any]:
    """The control plane. It drives the broker, so it needs the Docker socket — but the
    DockerBackend guard scopes every start/stop to `<project>-*`, so socket access can NOT
    reach the live ordo-ai-stack containers. The rendered config dir is mounted read-only
    so a runtime model switch re-renders in place (one write path stays inside the project)."""
    s = _svc(f"{project}/ops-controller:latest", net=net, env_file=env_file, secrets=True)
    s["volumes"] = [
        "/var/run/docker.sock:/var/run/docker.sock",  # broker start/stop (guard-scoped)
        "./:/config",                                 # ordo.yaml + rendered out/ (single write path)
    ]
    s["environment"] = {"ORDO_PROJECT": project}
    # --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
    s["command"] = ["--source", "/config/ordo.yaml", "serve", "--project", project, "--out", "/config/out"]
    return s


def _mcp_gateway(project: str, net: str, env_file: str) -> dict[str, Any]:
    """The MCP tool gateway. Like V1 it SPAWNS MCP servers as sibling containers, so it needs the
    Docker socket; and its wrapper reads the rendered catalog (mcp-registry.yaml) from a mounted
    config dir at runtime — not baked. Without the socket + the config mount + these env keys the
    gateway boots with an empty/UNKNOWN catalog and the agent has no tools (a live-only failure).
    GitHub/n8n API tokens for spawned servers come from secrets.env (env-var form)."""
    s = _svc(f"{project}/mcp-gateway:latest", net=net, env_file=env_file, secrets=True)
    s["volumes"] = [
        "/var/run/docker.sock:/var/run/docker.sock",  # gateway spawns MCP servers as containers
        # the rendered mcp config dir (servers.txt + registry-custom.yaml, wrapper-native schema). RW
        # because the wrapper writes registry-custom.docker.yaml (placeholder substitution) alongside.
        "./mcp:/mcp-config",
    ]
    s["environment"] = {
        "MCP_GATEWAY_PORT": "8811",
        # the wrapper reads the enabled server list from servers.txt and merges registry-custom.yaml.
        "MCP_CONFIG_FILE": "/mcp-config/servers.txt",
        "MCP_GATEWAY_VERBOSE": "1",
        "OPS_CONTROLLER_URL": "http://ops-controller:9000",
        "COMFYUI_URL": "http://comfyui:8188",
        "N8N_API_URL": "http://n8n:5678",
        "CODE_ROOT": "${CODE_ROOT:-/c/dev}",
    }
    s["healthcheck"] = {
        "test": ["CMD-SHELL", "sh /mcp-scripts/healthcheck.sh"],
        "interval": "15s", "timeout": "10s", "retries": 5, "start_period": "60s",
    }
    return s


def _apply_agent_runtime(svc: dict[str, Any], *, user: str | None, volumes: list[str] | None,
                         environment: dict[str, str] | None,
                         secret_files: list[dict[str, str]] | None,
                         depends_on: dict[str, str] | None,
                         healthcheck: dict[str, Any] | None) -> None:
    """Layer the agent manifest's runtime wiring onto the base agent service (in place). File
    secrets render as read-only bind mounts of the operator's host secret files into /run/secrets/*
    (the same files V1 mounts; independent of secrets.env). depends_on with conditions overrides the
    plain start-order list so V1's service_healthy gates are mirrored."""
    if user:
        svc["user"] = user
    vols = list(volumes or [])
    for sf in (secret_files or []):
        vols.append(f"{sf['source']}:{sf['target']}:ro")
    if vols:
        svc["volumes"] = vols
    if environment:
        svc["environment"] = dict(environment)
    dep = _depends_on(depends_on)
    if dep:
        svc["depends_on"] = dep  # long-form conditions replace the base plain list
    if healthcheck:
        svc["healthcheck"] = dict(healthcheck)


def _plugin_service(ps: "PluginService", plugin: "Plugin", *, net: str, env_file: str,
                    has_gpu: bool, primary_uuid: str | None, secondary_uuid: str | None,
                    project: str) -> dict[str, Any]:
    """Render ONE compose service from a plugin's declared PluginService — data-driven, so
    adding a service is a manifest edit, not a code change here. `${...}` / `./...` refs and
    named volumes pass straight through to compose (project-scoped, no live-stack collision)."""
    s: dict[str, Any] = {"image": ps.image, "restart": "unless-stopped", "networks": [net]}
    files = _env_files(env_file, ps.wants_secrets)
    if files:
        s["env_file"] = files
    if plugin.compose_profile:
        s["profiles"] = [plugin.compose_profile]
    env = dict(ps.env)
    # GPU wiring — BOTH layers (CUDA_VISIBLE_DEVICES + a device_ids reservation) on a real uuid,
    # because device_ids alone is a WSL2 no-op (see overrides/gpu-assignments.yml in V1):
    #   gpu_pin: secondary -> the non-primary card (voice STT/TTS → the Pascal 1070; no Blackwell)
    #   gpu_pin: primary   -> the compute card by uuid (comfyui/llamacpp-embed → the 5090). V1 pins
    #                         these explicitly; `count: all` here would let them see the 1070 too.
    if ps.gpu_pin == "secondary" and secondary_uuid:
        env.update(_pin_env(secondary_uuid))
        s.update(_gpu_pinned_reservation(secondary_uuid))
    elif ps.gpu_pin == "primary" and primary_uuid:
        env.update(_pin_env(primary_uuid))
        s.update(_gpu_pinned_reservation(primary_uuid))
    elif (ps.gpu or ps.gpu_pin) and has_gpu:
        # a GPU service on a machine whose primary uuid didn't resolve (CI/mock) — fall back to the
        # all-GPU reservation so the shape is still valid; the uuid pin is added when detect() has it.
        s.update(_GPU_RESERVATION)
    if env:
        s["environment"] = env
    if ps.command:
        s["command"] = list(ps.command)
    if ps.volumes:
        s["volumes"] = list(ps.volumes)
    if ps.healthcheck:
        s["healthcheck"] = dict(ps.healthcheck)
    dep = _depends_on(ps.depends_on)
    if dep:
        s["depends_on"] = dep
    if ps.ports:  # edge/front-door only (Caddy :443); gated behind the plugin's opt-in profile
        s["ports"] = list(ps.ports)
    return s


def render_compose(*, has_gpu: bool, compose_profiles: list[str], agent: str = "hermes",
                   project: str = "ordo-v2", env_file: str = ".env",
                   agent_image: str | None = None,
                   agent_command: list[str] | None = None,
                   agent_user: str | None = None,
                   agent_volumes: list[str] | None = None,
                   agent_environment: dict[str, str] | None = None,
                   agent_secret_files: list[dict[str, str]] | None = None,
                   agent_depends_on: dict[str, str] | None = None,
                   agent_healthcheck: dict[str, Any] | None = None,
                   llamacpp_image: str | None = None,
                   plugin_services: "list[tuple[Plugin, PluginService]] | None" = None,
                   primary_gpu_uuid: str | None = None,
                   secondary_gpu_uuid: str | None = None) -> dict[str, Any]:
    net = f"{project}-net"
    # the agent is swappable (Hermes is the default); a registry manifest may pin any image,
    # else fall back to the <project>/agent-<id>:latest convention.
    agent_img = agent_image or f"{project}/agent-{agent}:latest"
    # the llama.cpp image is the stock upstream build unless the chosen model pins a patched
    # one (e.g. Qwen3.6 SWA) via its catalog `backend_image` — flowed here through render.
    llamacpp_img = llamacpp_image or "ghcr.io/ggml-org/llama.cpp:server"
    llamacpp = _svc(llamacpp_img, net=net, env_file=env_file, gpu=has_gpu)
    # always-on Prometheus metrics endpoint (the monitoring plugin's prometheus scrapes it).
    llamacpp["command"] = [LLAMACPP_METRICS_ARG]
    # Pin the compute service to the PRIMARY card by uuid (V1 does this in gpu-assignments.yml).
    # Without the CUDA_VISIBLE_DEVICES pin, on a dual-GPU WSL2 box `count: all` lets llama.cpp see
    # the 1070 too — a live-only failure the beside-run rollbacks were exactly the shape of. The
    # `.env` still carries no pin; this is a compose-level env override on the service.
    if has_gpu and primary_gpu_uuid:
        llamacpp["deploy"] = _gpu_pinned_reservation(primary_gpu_uuid)["deploy"]
        llamacpp["environment"] = _pin_env(primary_gpu_uuid)
    # The patched image is a drop-in binary at /app/llama-server; the launch LOGIC lives in the
    # host wrapper scripts/llamacpp/run-llama-server.sh, which translates the rendered LLAMACPP_*
    # env into the full `llama-server -m /models/<gguf> -c <ctx> -ngl -1 …` argv. Without this
    # entrypoint + the two bind mounts, the image falls through to its default entrypoint and
    # boots in model-less "router mode" (0 models, no VRAM). GGUF weights + the wrapper are
    # shared-by-path from the V1 tree via ${BASE_PATH} (already rendered into .env), so no copy.
    llamacpp["entrypoint"] = ["/bin/sh", "/llamacpp-scripts/run-llama-server.sh"]
    llamacpp["volumes"] = [
        "${BASE_PATH:-.}/models/gguf:/models:ro",
        "${BASE_PATH:-.}/scripts/llamacpp:/llamacpp-scripts:ro",
    ]
    # model-gateway + mcp-gateway are V1 CUSTOM-BUILT config-wrapper images (LiteLLM + the
    # `local-chat` alias config; docker/mcp-gateway + the reload wrapper). V2 pins them as its own
    # project-namespaced BUILDABLE images (build contexts under v2/docker/{model-gateway,mcp-gateway})
    # so preflight reports 'build first' not 'Docker will pull' — matching the llamacpp-patched
    # precedent. The V2-native ops-controller + dashboard remain the new control plane.
    svcs: dict[str, Any] = {
        "llamacpp": llamacpp,
        # LITELLM_MASTER_KEY + THROUGHPUT_RECORD_TOKEN are secrets (from secrets.env).
        "model-gateway": _svc(f"{project}/model-gateway:latest", net=net, env_file=env_file,
                              depends=["llamacpp"], secrets=True),
        "mcp-gateway": _mcp_gateway(project, net, env_file),
        "ops-controller": _ops_controller(project, net, env_file),
        "dashboard": _svc(f"{project}/dashboard:latest", net=net, env_file=env_file,
                          depends=["ops-controller"], secrets=True),
        # OPS_CONTROLLER_TOKEN + Discord/backup tokens are secrets (from secrets.env).
        "agent": _svc(agent_img, net=net, env_file=env_file,
                      depends=["model-gateway", "mcp-gateway", "ops-controller"], secrets=True),
    }
    # The agent image's default CMD may be a no-op (agent-hermes defaults to `hermes --help`, which
    # prints usage and exits → restart loop). The manifest's `command` (Hermes: `hermes gateway`)
    # starts the persistent orchestrator; emit it so the rendered service overrides that default,
    # mirroring V1's compose. Empty -> omitted, so an agent whose image self-starts is unaffected.
    if agent_command:
        svcs["agent"]["command"] = list(agent_command)
    # Full agent runtime wiring (data-driven, from the agent manifest) — mirrors V1's hermes-gateway:
    # the brain bind (staged), /workspace/data, the /c/dev mirror, file secrets, env, service_healthy
    # depends, healthcheck. Each is emitted only when the manifest declares it (a self-contained
    # third-party agent that declares none renders exactly as before).
    _apply_agent_runtime(
        svcs["agent"], user=agent_user, volumes=agent_volumes, environment=agent_environment,
        secret_files=agent_secret_files, depends_on=agent_depends_on, healthcheck=agent_healthcheck)
    # optional plugin services, built from the resolved manifests (no hardcoded if-blocks).
    # render() only passes services whose plugin is enabled, so profile-gating already happened;
    # the per-service `profiles:` keeps them dormant until `--profile <p>` is used too.
    for plugin, ps in (plugin_services or []):
        svcs[ps.name] = _plugin_service(ps, plugin, net=net, env_file=env_file,
                                        has_gpu=has_gpu, primary_uuid=primary_gpu_uuid,
                                        secondary_uuid=secondary_gpu_uuid,
                                        project=project)

    out: dict[str, Any] = {"name": project, "services": svcs, "networks": {net: {"name": net}}}
    # Declare any named volumes the plugin services reference (a `src:dst` where src is a bare
    # name, not a ./bind or absolute path) — compose requires them in the top-level `volumes:`.
    named = _named_volumes(svcs)
    if named:
        out["volumes"] = {v: None for v in named}
    return out


def _named_volumes(svcs: dict[str, Any]) -> list[str]:
    """Collect bare-name volume sources (e.g. `prometheus-data:/prometheus`) needed at top level.
    Bind mounts (`./x:/y`, `/abs:/y`, `${VAR}/...`) and anonymous vols are skipped."""
    seen: list[str] = []
    for svc in svcs.values():
        for vol in svc.get("volumes", []) or []:
            if not isinstance(vol, str) or ":" not in vol:
                continue
            src = vol.split(":", 1)[0]
            if src and not src.startswith((".", "/", "~", "$")) and "/" not in src:
                if src not in seen:
                    seen.append(src)
    return seen


def core_services() -> list[str]:
    return list(_CORE)
