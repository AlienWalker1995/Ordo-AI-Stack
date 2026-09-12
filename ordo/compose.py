"""Render an ISOLATED docker-compose for the stack from the resolved config.

The isolation properties below are what let the stack stand on its own without
colliding with anything else on the host:
  - a dedicated project name + network (no collision with other compose projects),
  - NO host port publishes on core services (reached via the dashboard/agent, per the deployment
    model) so nothing fights other services' ports,
  - GPU reservations only when a GPU is present,
  - core services read the rendered .env (single source → no drift),
  - plugin services appear only behind their compose profile (media/voice),
  - each enabled MCP server is its own `mcp-<id>` service on an INTERNAL network that only
    model-gateway joins (no Docker socket, no env_file, capped CPU/memory).

The images/build contexts are the substrate's own; this renders the SHAPE and wiring. The
process broker starts/stops these against the scheduler.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import gpu

if TYPE_CHECKING:
    from .plugins import Plugin, PluginService

# The mandatory 6-service core (from the architecture decisions), plus the agent (added
# separately below) makes 7 mandatory services total. Caddy/oauth2-proxy is an OPTIONAL
# remote-access plugin, so it's not here — a local floor install is localhost-only. The
# enabled MCP servers render as `mcp-<id>` services alongside these, from the manifests.
_CORE = ["llamacpp", "litellm-db", "model-gateway", "model-gateway-keys",
         "ops-controller", "dashboard"]

# Build contexts for the SUBSTRATE services — the project images hardcoded below that have NO
# manifest (`_model_gateway`/`_ops_controller`, and the patched llama.cpp build
# pinned via a model's catalog `backend_image`). Manifest services (plugins/agents/dashboards,
# incl. the v1-parity dashboard's `ops-api` backend) declare their own context via `build:` in
# the manifest; only these hardcoded ones need to be declared here. `ordo.buildspec` reads this
# to give preflight + the substrate test a single image→context resolver — so a rename/typo fails
# CI, not deploy. Keyed by the image `repo/name` (matched on the `…-<name>` suffix too, for the
# `ordo-ai-stack-llamacpp-patched` build tag). This is build METADATA — never rendered into compose.
SUBSTRATE_BUILD_CONTEXTS: dict[str, str] = {
    "model-gateway": "services/model-gateway",
    "ops-controller": "services/ops-controller",
    "llamacpp-patched": "services/llamacpp-patched",
    # The generic GPU admission gate. Rendered as a companion service for any manifest service
    # declaring `gpu_arbitration.enforcement: gate` (see _gpu_gate below) — derived from the
    # declaration rather than declared per-plugin, so a second gated service needs no new image
    # and no new build path.
    "gpu-gate": "services/gpu-gate",
}

# --metrics turns on llama-server's native Prometheus endpoint at /metrics:8080 (token rates,
# queue depth). Always-on — it's cheap, and the monitoring plugin's prometheus scrapes it.
LLAMACPP_METRICS_ARG = "--metrics"

# LiteLLM's Postgres (virtual keys + spend; models/MCP stay in config.yaml). Pinned by digest.
POSTGRES_IMAGE = "postgres:16-alpine@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"


def _mcp_net(project: str) -> str:
    """The INTERNAL network shared by model-gateway and the MCP servers only (no other container
    can reach an MCP server directly; the servers reach the stack only if they also join
    `<project>-net`). `internal: true` at the top level = no default gateway, no egress."""
    return f"{project}-mcp-net"

_GPU_RESERVATION = {
    "deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]}}}
}


def _gpu_pinned_reservation(uuid: str) -> dict[str, Any]:
    """Reserve exactly one card by uuid (device_ids). Paired with CUDA_VISIBLE_DEVICES —
    both layers are required on Docker Desktop/WSL2 where device_ids alone is a no-op."""
    return {"deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "device_ids": [uuid], "capabilities": ["gpu"]}]}}}}


def _capability_gpu_reservation(capabilities: list[str]) -> dict[str, Any]:
    """An all-GPU (`count: all`, not a uuid pin — reads BOTH cards) reservation with the given
    NVIDIA capabilities. `["utility"]` injects `nvidia-smi` + NVML for read-only VRAM detection
    WITHOUT reserving compute; `["gpu"]` is a compute reservation. Data-driven so a service just
    declares the visibility it needs (see `_dashboard_backend`)."""
    return {"deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "count": "all", "capabilities": list(capabilities)}]}}}}


def _utility_gpu_reservation() -> dict[str, Any]:
    """A read-only GPU reservation: the `utility` capability injects `nvidia-smi` + NVML into
    the container WITHOUT reserving compute. The V2 scheduler (ops-controller) detects VRAM by
    shelling to nvidia-smi (hardware._detect_gpus); without this it sees CPU-only → total_vram=0
    → it can't do the VRAM-fit co-run admission that REPLACES V1's reactive guardian, and it
    drops every GPU plugin (comfyui/voice/worker) as 'not available'. V1's ops-controller has
    exactly this (caps=[[utility]]). `count: all` (not a uuid pin) so it can read BOTH cards."""
    return _capability_gpu_reservation(["utility"])


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
    reach containers outside this project. The rendered config dir is mounted read-only
    so a runtime model switch re-renders in place (one write path stays inside the project)."""
    s = _svc(f"{project}/ops-controller:latest", net=net, env_file=env_file, secrets=True)
    s["volumes"] = [
        "/var/run/docker.sock:/var/run/docker.sock",  # broker start/stop (guard-scoped)
        "./:/config",                                 # ordo.yaml + rendered out/ (single write path)
    ]
    s["environment"] = {"ORDO_PROJECT": project}
    # --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
    # --out is /config ITSELF: the deployment mounts the dir holding ordo.yaml AND the rendered
    # outputs (out) as /config, so an in-place re-render (model switch) must write next to the
    # source. "/config/out" nested into a dir nothing consumes — silent drift (found 2026-07-15).
    s["command"] = ["--source", "/config/ordo.yaml", "serve", "--project", project, "--out", "/config"]
    # Read-only GPU visibility so the scheduler can see real VRAM (mirrors V1's utility cap).
    s.update(_utility_gpu_reservation())
    s["environment"]["NVIDIA_DRIVER_CAPABILITIES"] = "utility"
    return s


def _litellm_db(net: str) -> dict[str, Any]:
    """Postgres for LiteLLM's virtual keys, teams and spend. Models and MCP servers stay in the
    rendered config (STORE_MODEL_IN_DB=False), so this holds only what the admin UI/keys need.
    No env_file: its single secret is interpolated by compose from secrets.env (--env-file), and
    an EMPTY password makes postgres refuse to start, which is the fail-loud we want."""
    return {
        "image": POSTGRES_IMAGE,
        "restart": "unless-stopped",
        "networks": [net],
        "environment": {
            "POSTGRES_USER": "litellm",
            "POSTGRES_DB": "litellm",
            "POSTGRES_PASSWORD": "${LITELLM_DB_PASSWORD}",
        },
        # named volume: DB state never rides the 9p bind (see the rag/qdrant notes)
        "volumes": ["litellm-db-data:/var/lib/postgresql/data"],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U litellm -d litellm"],
            "interval": "10s", "timeout": "5s", "retries": 5, "start_period": "20s",
        },
    }


def _model_gateway(project: str, net: str, env_file: str) -> dict[str, Any]:
    """LiteLLM behind the `local-chat` alias AND the MCP gateway (`/mcp`). The agent gates on
    `model-gateway: service_healthy` (audit G5), so this service MUST render a healthcheck or that
    gate is unsatisfiable and the agent never starts. Probe: GET /v1/models with the master key.

    Mounts the rendered out/model-gateway dir read-only: mcp_servers.yaml (the entrypoint merges it
    into the LiteLLM config) and keys.json (read by model-gateway-keys). Joins the internal MCP
    network so it can reach the mcp-* services. Secrets (LITELLM_MASTER_KEY, LITELLM_SALT_KEY,
    THROUGHPUT_RECORD_TOKEN) come from the secrets.env env_file and are NOT re-declared here."""
    s = _svc(f"{project}/model-gateway:latest", net=net, env_file=env_file, secrets=True)
    s["networks"] = [net, _mcp_net(project)]
    s["depends_on"] = _depends_on({"llamacpp": "service_started", "litellm-db": "service_healthy"})
    s["volumes"] = ["./model-gateway:/config:ro"]
    s["environment"] = {
        "LITELLM_MODE": "PRODUCTION",   # no load_dotenv(): a stray .env cannot inject credentials
        "LITELLM_LOG": "ERROR",
        "DATABASE_URL": "postgresql://litellm:${LITELLM_DB_PASSWORD}@litellm-db:5432/litellm",
        "STORE_MODEL_IN_DB": "False",   # config.yaml is the single source of truth for models + MCP
        # uvicorn only trusts X-Forwarded-Proto/Host from loopback by default; caddy connects from
        # a 172.x address on the project network, so without this the /ui slash redirect and the
        # post-login 303 come back http:// on a TLS-only port (reproduced 2026-09-12). No host
        # port is published, so the only peers that can reach this service are project services.
        "FORWARDED_ALLOW_IPS": "*",
    }
    s["healthcheck"] = {
        "test": ["CMD-SHELL", (
            "python3 -c \"import os, urllib.request; "
            "req = urllib.request.Request('http://localhost:11435/v1/models', "
            "headers={'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY']}); "
            "urllib.request.urlopen(req)\""
        )],
        "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "60s",
    }
    return s


def _model_gateway_keys(project: str, net: str, env_file: str) -> dict[str, Any]:
    """One-shot: provision the per-consumer LiteLLM virtual keys from the rendered keys.json
    (bootstrap_keys.py, idempotent). Same image as the gateway (no second build), runs after the
    gateway is healthy, exits 0 when the desired state holds; `on-failure` retries transient API
    errors. The agent depends on `service_completed_successfully` so Hermes never starts keyless."""
    s = _svc(f"{project}/model-gateway:latest", net=net, env_file=env_file, secrets=True)
    s["restart"] = "on-failure"
    s["command"] = ["python3", "/app/bootstrap_keys.py"]
    s["volumes"] = ["./model-gateway:/config:ro"]
    s["environment"] = {
        "MODEL_GATEWAY_URL": "http://model-gateway:11435",
        "LITELLM_KEYS_SPEC": "/config/keys.json",
    }
    s["depends_on"] = _depends_on({"model-gateway": "service_healthy"})
    return s


def _dashboard(project: str, net: str, env_file: str,
               dashboard: dict[str, Any] | None = None) -> dict[str, Any]:
    """The control-plane UI service. The dashboard is PLUGGABLE (data-driven, like the agent):
    the selected `dashboard` manifest supplies the image, env, depends_on and healthcheck. When no
    selection is passed (bare/legacy call) it falls back to the V2-native SPA defaults.

    V1's dashboard declares a container HEALTHCHECK on `/api/health`, and the agent gates on
    `dashboard: service_healthy` (audit G5). Keeping a healthcheck on THIS service is REQUIRED or
    that gate is unsatisfiable and the agent never starts — so a manifest that omits one still gets
    the V2-native curl probe as a floor."""
    dashboard = dashboard or {}
    image = dashboard.get("image") or f"{project}/dashboard:latest"
    wants_secrets = dashboard.get("wants_secrets", True)
    # depends_on: manifest may map {peer: condition}; default to start-ordering on ops-controller.
    depends = dashboard.get("depends_on") or {"ops-controller": "service_started"}
    s = _svc(image, net=net, env_file=env_file, secrets=wants_secrets)
    dep = _depends_on(depends)
    if dep:
        s["depends_on"] = dep
    env = dashboard.get("environment") or {}
    if env:
        s["environment"] = dict(env)
    # GPU visibility for the dashboard SERVICE: the V1-parity dashboard's `/api/hardware` shells to
    # nvidia-smi (_probe_gpu) + enumerates cards (gpu_stats.list_gpus) for the hw-stat bar's GPU
    # widgets, which the NVIDIA runtime only injects when the service reserves a GPU with the
    # `utility` cap. Without it `/api/hardware` returns gpu:null + gpus:[] (both GPU widgets blank).
    # V1's dashboard container has exactly caps=[[utility]]; mirror it. `count: all` -> reads BOTH cards.
    gpu_caps = dashboard.get("gpu_capabilities") or []
    if gpu_caps:
        s.update(_capability_gpu_reservation(list(gpu_caps)))
    if dashboard.get("volumes"):
        s["volumes"] = list(dashboard["volumes"])
    s["healthcheck"] = dashboard.get("healthcheck") or {
        "test": ["CMD-SHELL", "curl -sf http://localhost:8080/api/health || exit 1"],
        "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "30s",
    }
    return s


def _dashboard_backend(net: str, env_file: str, backend: dict[str, Any]) -> dict[str, Any]:
    """Render the OPTIONAL companion backend a dashboard manifest declares (e.g. the V1-parity
    `ops-api` control API). Fully data-driven — image/env/volumes/depends/healthcheck come straight
    from the manifest. `group_add_root` mirrors V1's ops-controller `group_add: ["0"]` for
    Docker-socket access on Docker Desktop (root:root socket)."""
    s = _svc(backend["image"], net=net, env_file=env_file,
             secrets=backend.get("wants_secrets", True))
    if backend.get("group_add_root"):
        s["group_add"] = ["0"]
    # GPU visibility: the V1-parity `ops-api` backend enumerates GPUs by shelling to nvidia-smi
    # (it's a copy of V1's ops-controller), which the NVIDIA runtime only injects when the service
    # reserves a GPU with the `utility` capability. Without this the backend sees ZERO GPUs and the
    # dashboard's GPU widgets report "No GPUs returned from registry". `count: all` reads both cards.
    gpu_caps = backend.get("gpu_capabilities") or []
    if gpu_caps:
        s.update(_capability_gpu_reservation(list(gpu_caps)))
    if backend.get("environment"):
        s["environment"] = dict(backend["environment"])
    if backend.get("volumes"):
        s["volumes"] = list(backend["volumes"])
    dep = _depends_on(backend.get("depends_on"))
    if dep:
        s["depends_on"] = dep
    if backend.get("healthcheck"):
        s["healthcheck"] = dict(backend["healthcheck"])
    return s


def _mcp_service(server: dict[str, Any], *, net: str, mcp_net: str) -> dict[str, Any]:
    """ONE MCP server as a long-lived compose service, from its render record (ordo/render._render_mcp).
    Isolation parity with what the retired Docker gateway spawned (no-new-privileges, 1 CPU / 2 GB,
    init) plus: NO env_file (only the manifest's declared env reaches it), the internal MCP network
    (only model-gateway can call it), `stack` network only when it must reach another service, and
    labels ops-api uses to list MCP services. LiteLLM dials http://mcp-<id>:<port><path>."""
    s: dict[str, Any] = {
        "image": server["image"],
        "restart": "unless-stopped",
        "init": True,
        "networks": [mcp_net] if server["network"] == "internal" else [mcp_net, net],
        "security_opt": ["no-new-privileges:true"],
        "deploy": {"resources": {"limits": {"cpus": "1", "memory": "2g"}}},
        "labels": {
            "ordo.mcp": "true",
            "ordo.mcp.server_id": server["id"],
            "ordo.mcp.plugin": server["plugin_id"],
        },
        "healthcheck": dict(server["healthcheck"]),
    }
    if server["env"]:
        s["environment"] = dict(server["env"])
    if server["command"]:
        s["command"] = list(server["command"])
    if server["volumes"]:
        s["volumes"] = list(server["volumes"])
    if server["depends_on"]:
        s["depends_on"] = list(server["depends_on"])
    return s


def _apply_agent_runtime(svc: dict[str, Any], *, user: str | None, group_add: list[str] | None,
                         volumes: list[str] | None,
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
    if group_add:
        svc["group_add"] = list(group_add)
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


def _plugin_service(ps: PluginService, plugin: Plugin, *, net: str, env_file: str,
                    has_gpu: bool, primary_uuid: str | None, secondary_uuid: str | None,
                    project: str) -> dict[str, Any]:
    """Render ONE compose service from a plugin's declared PluginService — data-driven, so
    adding a service is a manifest edit, not a code change here. `${...}` / `./...` refs and
    named volumes pass straight through to compose (project-scoped, no live-stack collision)."""
    s: dict[str, Any] = {"image": ps.image, "restart": "unless-stopped", "networks": [net]}
    if ps.network_mode:
        # compose forbids networks: alongside network_mode: — the service lives in the
        # target's namespace (e.g. the tailnet-name sidecars inside Caddy's netns).
        s.pop("networks")
        s["network_mode"] = ps.network_mode
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
    if ps.shm_size:  # bump /dev/shm past docker's 64MB default (Electron/Selkies streaming needs it)
        s["shm_size"] = ps.shm_size
    return s


def _gpu_gate(ps: PluginService, plugin: Plugin, claim: Any, *, net: str, env_file: str,
              project: str) -> tuple[str, dict[str, Any]]:
    """Render the admission gate that fronts a `gpu_arbitration.enforcement: gate` service.

    Derived entirely from the declaration — one generic image, no per-service code. The gate
    listens on the upstream's port under the name `<service>-gate`, so pointing a consumer at it
    is a hostname change and nothing else. It reserves NO GPU: it is an HTTP proxy that acquires
    residency from ops-controller before letting a submission through, and it is a CLIENT of the
    arbiter — it never starts or stops another container.
    """
    g = ps.gpu_arbitration.gate
    name = gpu.gate_service_name(ps.name)
    s: dict[str, Any] = {
        "image": f"{project}/gpu-gate:latest",
        "restart": "unless-stopped",
        "networks": [net],
        # secrets.env carries OPS_CONTROLLER_TOKEN; .env carries nothing the gate needs, but the
        # layering matches every other service (and keeps ${...} refs resolvable).
        "env_file": _env_files(env_file, True),
        "depends_on": [ps.name],
        "environment": {
            "GATE_UPSTREAM": f"http://{ps.name}:{g.upstream_port}",
            "GATE_LISTEN_PORT": str(g.listen_port),
            "GATE_SUBMIT_PATHS": ",".join(g.submit_paths),
            "GATE_SUBMIT_METHODS": ",".join(g.submit_methods),
            "GATE_QUEUE_PATH": g.queue_path,
            "GATE_QUEUE_STYLE": g.queue_style,
            "GATE_DRAIN_SECONDS": str(g.drain_seconds),
            "OPS_CONTROLLER_URL": "http://ops-controller:9000",
            # The lease contract, shared verbatim with assets/lease-exec.py — one env vocabulary
            # for every client of the arbiter, so there is no second way to ask for the GPU.
            "ORDO_LEASE_VRAM_GB": str(claim.vram_gb),
            "ORDO_LEASE_KIND": claim.kind,
            "ORDO_LEASE_JOB_ID": f"gate-{ps.name}",
            "ORDO_LEASE_EST_SECONDS": str(claim.est_seconds),
            "ORDO_LEASE_ACQUIRE_TIMEOUT_S": str(g.acquire_timeout_seconds),
        },
        "healthcheck": {
            "test": ["CMD", "python", "-c",
                     "import urllib.request,sys;"
                     f"sys.exit(0 if urllib.request.urlopen('http://localhost:{g.listen_port}"
                     "/_gpu_gate/health', timeout=5).status == 200 else 1)"],
            "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "20s",
        },
    }
    if plugin.compose_profile:
        # The gate rides its upstream's profile: a dormant service must not get a live gate, and
        # an enabled service must never come up without one.
        s["profiles"] = [plugin.compose_profile]
    return name, s


def render_compose(*, has_gpu: bool, compose_profiles: list[str], agent: str = "hermes",
                   project: str = "ordo", env_file: str = ".env",
                   agent_image: str | None = None,
                   agent_command: list[str] | None = None,
                   agent_user: str | None = None,
                   agent_group_add: list[str] | None = None,
                   agent_volumes: list[str] | None = None,
                   agent_environment: dict[str, str] | None = None,
                   agent_secret_files: list[dict[str, str]] | None = None,
                   agent_depends_on: dict[str, str] | None = None,
                   agent_healthcheck: dict[str, Any] | None = None,
                   dashboard: dict[str, Any] | None = None,
                   llamacpp_image: str | None = None,
                   plugin_services: list[tuple[Plugin, PluginService]] | None = None,
                   primary_gpu_uuid: str | None = None,
                   secondary_gpu_uuid: str | None = None,
                   gpu_claims: dict[str, Any] | None = None,
                   mcp_servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    net = f"{project}-net"
    # the agent is swappable (Hermes is the default); a registry manifest may pin any image,
    # else fall back to the <project>/agent-<id>:latest convention.
    agent_img = agent_image or f"{project}/agent-{agent}:latest"
    # the llama.cpp image is the stock upstream build unless the chosen model pins a patched
    # one (e.g. Qwen3.6 SWA) via its catalog `backend_image` — flowed here through render.
    # Pinned to the digest of the same stock image running as ordo-llamacpp-embed-1, resolved
    # 2026-07-24 (re-resolve via `docker inspect ordo-llamacpp-embed-1` on bump).
    llamacpp_img = (llamacpp_image
                     or "ghcr.io/ggml-org/llama.cpp:server@sha256:295dc9897fa8a643e4a513fbcaada51d3b8db4b0afa4fda7aeae2386757de58b")
    llamacpp = _svc(llamacpp_img, net=net, env_file=env_file, gpu=has_gpu)
    # always-on Prometheus metrics endpoint (the monitoring plugin's prometheus scrapes it).
    llamacpp["command"] = [LLAMACPP_METRICS_ARG]
    # Pin the compute service to the PRIMARY card by uuid (V1 does this in gpu-assignments.yml).
    # Without the CUDA_VISIBLE_DEVICES pin, on a dual-GPU WSL2 box `count: all` lets llama.cpp see
    # the 1070 too — a failure that only surfaces against real dual-GPU hardware. The
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
        # models-gguf NAMED VOLUME, not the old ${BASE_PATH}/models/gguf 9p bind: on
        # 2026-08-07 the 9p mount began wedging GGUF reads deterministically
        # (p9_client_rpc D-state at ~100MB into the file, fresh VM, first contact),
        # eventually crashing the whole Docker VM. Third 9p casualty after the Hermes
        # brain (#143) and comfyui-storage (#156). Adding a model now means `ordo fetch`
        # to the host staging dir then copying into the volume (docker cp via a helper
        # container — the dashboard GGUF-pull endpoint is still a 501 stub, see
        # docs/data.md "Model Pull") — models/gguf is retired from every hot path.
        "models-gguf:/models:ro",
        "${BASE_PATH:-.}/scripts/llamacpp:/llamacpp-scripts:ro",
    ]
    # model-gateway is the V1 custom-built LiteLLM config wrapper (+ the MCP gateway since 2026-09);
    # pinned as a project-namespaced BUILDABLE image (build context services/model-gateway) so
    # preflight reports 'build first' not 'Docker will pull' — matching the llamacpp-patched
    # precedent. The V2-native ops-controller + dashboard remain the new control plane.
    svcs: dict[str, Any] = {
        "llamacpp": llamacpp,
        "litellm-db": _litellm_db(net),
        # LITELLM_MASTER_KEY + LITELLM_SALT_KEY + THROUGHPUT_RECORD_TOKEN are secrets (secrets.env).
        "model-gateway": _model_gateway(project, net, env_file),
        "model-gateway-keys": _model_gateway_keys(project, net, env_file),
        "ops-controller": _ops_controller(project, net, env_file),
        # The dashboard is pluggable (data-driven): the selected manifest supplies image/env/
        # depends/healthcheck. A manifest may also declare a companion backend (e.g. the V1-parity
        # `ops-api`) which is rendered as its OWN service below — keeping V2's `ordo serve` service
        # named `ops-controller` (its live clients depend on that name) collision-free.
        "dashboard": _dashboard(project, net, env_file, dashboard),
        # OPS_CONTROLLER_TOKEN + Discord/backup tokens are secrets (from secrets.env).
        "agent": _svc(agent_img, net=net, env_file=env_file,
                      depends=["model-gateway", "model-gateway-keys", "ops-controller"], secrets=True),
    }
    # Optional dashboard backend (e.g. ops-api for the V1-parity dashboard) — rendered verbatim.
    if dashboard and dashboard.get("backend"):
        b = dashboard["backend"]
        svcs[b["name"]] = _dashboard_backend(net, env_file, b)
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
        svcs["agent"], user=agent_user, group_add=agent_group_add, volumes=agent_volumes,
        environment=agent_environment, secret_files=agent_secret_files,
        depends_on=agent_depends_on, healthcheck=agent_healthcheck)
    # optional plugin services, built from the resolved manifests (no hardcoded if-blocks).
    # render() only passes services whose plugin is enabled, so profile-gating already happened;
    # the per-service `profiles:` keeps them dormant until `--profile <p>` is used too.
    claims = gpu_claims or {}
    for plugin, ps in (plugin_services or []):
        svcs[ps.name] = _plugin_service(ps, plugin, net=net, env_file=env_file,
                                        has_gpu=has_gpu, primary_uuid=primary_gpu_uuid,
                                        secondary_uuid=secondary_gpu_uuid,
                                        project=project)
        # A service whose GPU use is gate-enforced gets its gate rendered WITH it, from the same
        # declaration. Not opt-in and not a separate manifest entry: the two cannot disagree, and
        # an enabled gated service can never come up without the thing that arbitrates it.
        arb = ps.gpu_arbitration
        if arb is not None and arb.enforcement == "gate" and ps.name in claims:
            gate_name, gate_svc = _gpu_gate(ps, plugin, claims[ps.name], net=net,
                                            env_file=env_file, project=project)
            svcs[gate_name] = gate_svc

    # MCP servers: one compose service per image-backed record; hosted (url-only) servers render
    # nothing here (LiteLLM dials them directly).
    for server in (mcp_servers or []):
        if not server["hosted"]:
            svcs[server["service"]] = _mcp_service(server, net=net, mcp_net=_mcp_net(project))

    out: dict[str, Any] = {
        "name": project,
        "services": svcs,
        "networks": {net: {"name": net}, _mcp_net(project): {"name": _mcp_net(project), "internal": True}},
    }
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
