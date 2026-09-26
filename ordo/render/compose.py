"""Render an ISOLATED docker-compose for the stack from the resolved config.

The isolation properties below are what let the stack stand on its own without
colliding with anything else on the host:
  - a dedicated project name + network (no collision with other compose projects),
  - NO host port publishes on core services (reached via the dashboard/agent, per the deployment
    model) so nothing fights other services' ports; the one exception is a UI's declared
    `local_port`, published on 127.0.0.1 only and only while the edge is off,
  - NVIDIA GPU reservations only when the compute GPU is NVIDIA (the one vendor with a compose
    device driver); other GPUs reach llama.cpp as passed-through device nodes,
  - no service loads the rendered .env whole: each receives only the derived keys it reads, as
    `KEY: ${KEY?...}` refs compose interpolates from `--env-file .env` (single source, no drift,
    and a changed key recreates only its readers),
  - plugin services appear only behind their compose profile (media/voice),
  - each enabled MCP server is its own `mcp-<id>` service on an INTERNAL network that only
    model-gateway joins (no Docker socket, no env_file, capped CPU/memory),
  - each `gpu_arbitration.enforcement: gate` upstream (ComfyUI) sits on a private network that
    only its admission gate joins, so the gate is the one route to it (see gated_upstream_net).

The images/build contexts are the substrate's own; this renders the SHAPE and wiring. The
process broker starts/stops these against the scheduler.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from . import gpu
from .secret_files import SecretFileRef
from .secret_files import add_to_service as _add_secret_files

if TYPE_CHECKING:
    from .llamacpp_backend import LlamaCppBackend
    from .plugins import Plugin, PluginService

# The mandatory 6-service core (from the architecture decisions), plus the agent (added
# separately below) makes 7 mandatory services total. Caddy/oauth2-proxy is an OPTIONAL
# remote-access plugin, so it's not here — a local floor install is localhost-only. The
# enabled MCP servers render as `mcp-<id>` services alongside these, from the manifests.
_CORE = ["llamacpp", "litellm-db", "model-gateway", "model-gateway-keys",
         "ops-controller", "dashboard"]

# Build contexts for the SUBSTRATE images: the project images with NO manifest (`_model_gateway`,
# `_ops_controller`, the gpu-gate, and the patched llama.cpp build a model's catalog `backend_image`
# names). Manifest services (plugins/agents/dashboards) declare their own context via `build:` in
# the manifest; only these need to be declared here. `buildspec.py` reads this to give preflight +
# the substrate test a single image→context resolver, so a rename/typo fails CI, not deploy. Keyed
# by the image name under the project namespace. This is build METADATA, never rendered into compose.
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
# The substrate images `ordo build` builds and render tags. Each is declared UNTAGGED (by compose.py,
# or for llamacpp-patched by a model's catalog `backend_image`): render fills in the tag `ordo build`
# recorded (ordo/render/image_tags.py).
SUBSTRATE_IMAGES: tuple[str, ...] = ("model-gateway", "ops-controller", "gpu-gate", "llamacpp-patched")

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

def gated_upstream_net(project: str, service: str) -> str:
    """The private network of a gate-enforced service: shared by that service and its admission
    gate ONLY. Nothing else can resolve `<service>:<port>`, so every caller has to go through the
    gate, which takes the GPU lease before it forwards a submission. Before this, ComfyUI sat on
    `<project>-net` and the gate was a convention: Hermes scripts submitted straight to
    comfyui:8188, one with no lease at all. A plain bridge, not `internal`: the upstream keeps its
    egress for model and custom-node downloads; isolation here is about who can reach IT."""
    return f"{project}-{service}-net"


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
    declares the visibility it needs."""
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


# Secrets a service may read that are deliberately NOT required (not in secrets.env.example):
# the dashboard only enforces THROUGHPUT_RECORD_TOKEN "when set" (see the note in engine.py's CORE_SECRET_KEYS).
# A service passes these as ${KEY:-} so an absent value is simply empty; materialize writes one
# into secrets.env only when the store holds it.
OPTIONAL_SECRET_KEYS: tuple[str, ...] = ("THROUGHPUT_RECORD_TOKEN",)


# Operator-managed secrets live in secrets.env (SOPS-decrypted / hand-filled), NEVER in the rendered
# .env, and no service loads that file whole. Each service lists the secret NAMES it reads. Where the
# software can read a file, the secret is file-delivered (`secret_files:`, ordo/render/secret_files.py): a
# read-only /run/secrets mount and only its path in the environment. Otherwise it renders as
# `KEY: ${KEY}` and compose interpolates the value from `--env-file secrets.env`. Either way a
# service holds only the secrets it declares.
def _secret_env(names) -> dict[str, str]:
    """`KEY: ${KEY}` per secret name. An optional one (OPTIONAL_SECRET_KEYS) becomes ${KEY:-} so an
    absent value is empty rather than a compose warning; a required one stays ${KEY}, so a missing
    value is reported."""
    return {n: (f"${{{n}:-}}" if n in OPTIONAL_SECRET_KEYS else f"${{{n}}}") for n in names}


def _add_secrets(s: dict[str, Any], names) -> None:
    """Merge secret refs into a service's environment. A value the manifest already set explicitly
    (e.g. `OPS_CONTROLLER_TOKEN: ${OPS_CONTROLLER_TOKEN:-}`) is kept as written."""
    if not names:
        return
    env = s.setdefault("environment", {})
    for k, v in _secret_env(names).items():
        env.setdefault(k, v)


# Derived config (the rendered out/.env) follows the same rule as secrets: no service loads the file
# (no `env_file`). A service lists the derived NAMES it reads (`derived_env:` in its manifest, or the
# lists below for the core services) and each renders as `KEY: ${KEY?...}`, which compose interpolates
# from `--env-file .env`. A render that changes one key therefore changes the config hash of exactly
# the services that read it. With a whole-file env_file it changed every service, so the next `up`
# recreated the whole stack (GPU residents included) over a key most of them never read.
#
# A key the render did not produce this time (COMFYUI_URL without comfyui, CADDY_* without the edge,
# a `site:` knob the operator did not set) is left out, exactly as the whole-file env_file left it out.
# `?` (not `:?`) fails the compose call when a declared key is missing from .env, while still
# allowing a legitimately empty value (LLAMACPP_MMPROJ on a text-only model).
#
# The keys each core service reads, taken from its entrypoint.
# llama.cpp: scripts/llamacpp/run-llama-server.sh, which turns these into the llama-server argv.
LLAMACPP_DERIVED_ENV: tuple[str, ...] = (
    "LLAMACPP_MODEL", "LLAMACPP_CTX_SIZE", "LLAMACPP_PARALLEL", "LLAMACPP_ROPE_SCALING",
    "LLAMACPP_ROPE_SCALE", "LLAMACPP_YARN_ORIG_CTX", "LLAMACPP_GPU_LAYERS", "LLAMACPP_FLASH_ATTN",
    "LLAMACPP_N_PREDICT", "LLAMACPP_REASONING_BUDGET", "LLAMACPP_MMPROJ",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION", "LLAMACPP_KV_CACHE_TYPE_K", "LLAMACPP_KV_CACHE_TYPE_V",
    "LLAMACPP_EXTRA_ARGS",
    "LLAMACPP_OVERRIDE_KV",  # an optional `site:` knob (--override-kv), absent unless set
)
# model-gateway: services/model-gateway/entrypoint.sh, which writes these into the LiteLLM config's
# model_info (context window, max output, weights names, vision, per-token cost). The CPU and embed
# model names are `site:` knobs, present only when the operator sets them.
MODEL_GATEWAY_DERIVED_ENV: tuple[str, ...] = (
    "LLAMACPP_CTX_SIZE", "LLAMACPP_N_PREDICT", "LLAMACPP_CPU_CTX", "LLAMACPP_MODEL",
    "LLAMACPP_CPU_MODEL", "LLAMACPP_EMBED_MODEL", "LLAMACPP_IMAGE", "LLAMACPP_MMPROJ",
    "LOCAL_INPUT_COST_PER_TOKEN", "LOCAL_OUTPUT_COST_PER_TOKEN",
)


def _derived_env(names, available_env) -> dict[str, str]:
    """`KEY: ${KEY?...}` per declared derived name the render produced. `available_env` is the set of
    keys in the rendered .env; None (a bare render_compose call) emits every declared name."""
    return {n: f"${{{n}?{n} is missing from the rendered .env}}" for n in names
            if available_env is None or n in available_env}


def _add_derived_env(s: dict[str, Any], names, available_env) -> None:
    """Merge derived-config refs into a service's environment. A value the service already sets
    explicitly is kept as written (a manifest may not declare a name in both places, see
    plugins.parse_derived_env)."""
    refs = _derived_env(names, available_env)
    if not refs:
        return
    env = s.setdefault("environment", {})
    for k, v in refs.items():
        env.setdefault(k, v)


def _svc(image: str, *, net: str, gpu: bool = False,
         profiles: list[str] | None = None, depends: list[str] | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"image": image, "restart": "unless-stopped", "networks": [net]}
    if profiles:
        s["profiles"] = profiles
    if depends:
        s["depends_on"] = depends
    if gpu:
        s.update(_GPU_RESERVATION)
    return s


def _ops_controller(project: str, net: str, nvidia_gpu: bool) -> dict[str, Any]:
    """The control plane. It drives the broker, so it needs the Docker socket — but the
    DockerBackend guard scopes every start/stop to `<project>-*`, so socket access can NOT
    reach containers outside this project. The rendered config dir is mounted read-only
    so a runtime model switch re-renders in place (one write path stays inside the project)."""
    # No derived env, and no secret but its own bearer token. It reads .env and secrets.env as FILES
    # (--env-file) for compose interpolation, never from its process env: compose prefers a process
    # env value over --env-file, so a derived key in its env would shadow the fresh .env its own
    # re-render just wrote (a model switch would recreate llama.cpp with the old values).
    s = _svc(f"{project}/ops-controller", net=net)
    s["volumes"] = [
        "/var/run/docker.sock:/var/run/docker.sock",  # broker start/stop (guard-scoped)
        # ordo.yaml + rendered out/ (single write path), by HOST path like every other bind: the
        # post-render step hashes every service from in here with /config as the project directory,
        # and a "./" bind would hash differently there than on the host (read as changed forever).
        "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out:/config",
        "${DATA_PATH:?DATA_PATH must be set (non-empty)}/ops-controller:/data",  # audit log, scheduler state
        "comfyui-models:/models/comfyui",             # shared ComfyUI model store (same as ops-api)
        # ComfyUI's app tree, read-only: /comfyui/install-node-requirements has to see whether a
        # custom-node pack ships a requirements.txt before it runs pip inside the comfyui
        # container. ops-api read this from ./data/comfyui-storage, which is BOTH the retired
        # pre-named-volume rollback copy AND resolved against out/ rather than the repo, so that
        # route answered 404 for every pack. The nodes only ever live in this volume.
        "comfyui-app:/comfyui-app:ro",
    ]
    s["environment"] = {
        "ORDO_PROJECT": project,
        "COMFYUI_MODELS_DIR": "/models/comfyui",
        "COMFYUI_CUSTOM_NODES_DIR": "/comfyui-app/ComfyUI/custom_nodes",
        "COMFYUI_CONTAINER_NAME": f"{project}-comfyui-1",
        "AUDIT_LOG_PATH": "/data/audit.log",
        # The GPU lease and eviction state, written on every transition so a recreate mid-lease
        # adopts it instead of forgetting it. `ordo recreate ops-controller` checks for this key.
        "SCHEDULER_STATE_PATH": "/data/scheduler-state.json",
    }
    # --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
    # --out is /config ITSELF: the deployment mounts the dir holding ordo.yaml AND the rendered
    # outputs (out) as /config, so an in-place re-render (model switch) must write next to the
    # source. "/config/out" nested into a dir nothing consumes — silent drift (found 2026-07-15).
    s["command"] = ["--source", "/config/ordo.yaml", "serve", "--project", project, "--out", "/config"]
    # Read-only GPU visibility so the scheduler can see real VRAM (mirrors V1's utility cap).
    # NVIDIA hosts only: without the NVIDIA runtime compose refuses the device request, and the
    # agent depends on this service. On a CPU host nvidia-smi is absent and detect() sees no GPU.
    if nvidia_gpu:
        s.update(_utility_gpu_reservation())
        s["environment"]["NVIDIA_DRIVER_CAPABILITIES"] = "utility"
    # Its own bearer token only, as a file (ordo/control/serve.py reads it with ordo.secret_env).
    _add_secret_files(s, [SecretFileRef("OPS_CONTROLLER_TOKEN", "OPS_CONTROLLER_TOKEN_FILE")])
    return s


def _litellm_db(net: str) -> dict[str, Any]:
    """Postgres for LiteLLM's virtual keys, teams and spend. Models and MCP servers stay in the
    rendered config (STORE_MODEL_IN_DB=False), so this holds only what the admin UI/keys need.
    Its password is a file: the postgres entrypoint reads POSTGRES_PASSWORD_FILE (docker-entrypoint.sh
    `file_env`), and only on the first start of an empty data directory (initdb). An EMPTY password
    makes that first start refuse, which is the fail-loud we want."""
    s = {
        "image": POSTGRES_IMAGE,
        "restart": "unless-stopped",
        "networks": [net],
        "environment": {
            "POSTGRES_USER": "litellm",
            "POSTGRES_DB": "litellm",
        },
        # named volume: DB state never rides the 9p bind (see the rag/qdrant notes)
        "volumes": ["litellm-db-data:/var/lib/postgresql/data"],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U litellm -d litellm"],
            "interval": "10s", "timeout": "5s", "retries": 5, "start_period": "20s",
        },
    }
    _add_secret_files(s, [SecretFileRef("LITELLM_DB_PASSWORD", "POSTGRES_PASSWORD_FILE")])
    return s


# Gateway-wide Langfuse tracing: the env model-gateway gets ONLY when the langfuse plugin is enabled
# (render_compose's `langfuse_tracing`). Every value was checked against the installed LiteLLM
# 1.100.1 source and a live probe (2026-09-15):
#   LITELLM_EXTRA_CALLBACKS  appended to litellm_settings.callbacks by services/model-gateway/
#                            add_callbacks.py; the template itself never names an optional plugin.
#   LANGFUSE_OTEL_HOST       read by integrations/langfuse/langfuse_otel.py (`<host>/api/public/otel`);
#                            the internal service URL, never the SSO edge (it would 302 the exporter).
#   LANGFUSE_TRACING_ENVIRONMENT  stamped as `langfuse.environment` on every span, so gateway traces
#                            are separable from Hermes's own (`hermes`).
#   OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content  suppresses ONLY the extra
#                            `raw_gen_ai_request` child span, which repeats the whole prompt and
#                            response as provider attributes. The Langfuse generation's input/output
#                            are written unconditionally by the langfuse_otel attribute setter, so
#                            they stay populated (verified: one observation per call, input non-null).
# LITELLM_OTEL_V2 is deliberately NOT set: on 1.100.1 the V2 path ignores LANGFUSE_TRACING_ENVIRONMENT
# (traces land in `default`), makes the root observation a proxy span with a NULL input, and exports
# every Postgres auth/spend call as its own observation. The project key pair (LANGFUSE_PUBLIC_KEY /
# LANGFUSE_SECRET_KEY) is a file secret, like every other model-gateway secret (see _model_gateway).
GATEWAY_LANGFUSE_ENV: dict[str, str] = {
    "LITELLM_EXTRA_CALLBACKS": "langfuse_otel",
    "LANGFUSE_OTEL_HOST": "http://langfuse-web:3000",
    "LANGFUSE_TRACING_ENVIRONMENT": "gateway",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "no_content",
}


# The rendered gateway config, bound by HOST path. ops-controller recreates model-gateway (a
# model switch does) running compose with its project directory at its own /config mount, so a
# "./model-gateway" bind would resolve to /config/model-gateway on the host, which does not exist.
# ":?" fails the compose call loudly if BASE_PATH is unset, instead of binding an empty
# /out/model-gateway that Docker would create and the gateway would start without config.
_MODEL_GATEWAY_CONFIG_BIND = "${BASE_PATH:?BASE_PATH must be set}/out/model-gateway:/config:ro"

# model-gateway (LiteLLM, the MCP fragment) and model-gateway-keys (the key grants) read the rendered
# out/model-gateway/ files at startup only, through a bind whose content compose does not hash. Both
# carry the digest of that content as a label, so a render that changes the files (an MCP toggle, a
# new key grant) changes their config hash and the changed set (ordo/render/changed_set.py) recreates
# them, from the host's `ordo apply` and from ops-controller's post-render step alike.
RENDERED_CONFIG_LABEL = "ordo.rendered-config"
MODEL_GATEWAY_CONFIG_READERS = ("model-gateway", "model-gateway-keys")


def rendered_config_digest(files: dict[str, str]) -> str:
    """The sha256 of a rendered config directory: each file's name and text, in name order."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0" + files[name].encode("utf-8") + b"\0")
    return digest.hexdigest()


# The secrets model-gateway reads, all as files: its entrypoint exports each one from its file
# (services/model-gateway/secret-env.sh) before LiteLLM starts, so the values live in the LiteLLM
# process environment and never in the container config. DATABASE_PASSWORD is LiteLLM's own name:
# with DATABASE_HOST/USERNAME/NAME it builds DATABASE_URL itself (litellm/proxy/utils.py
# construct_database_url_from_env_vars, quote_plus-escaped), so the password is never part of a
# rendered URL.
MODEL_GATEWAY_SECRET_FILES: tuple[SecretFileRef, ...] = (
    SecretFileRef("LITELLM_MASTER_KEY", "LITELLM_MASTER_KEY_FILE"),
    SecretFileRef("LITELLM_SALT_KEY", "LITELLM_SALT_KEY_FILE"),
    SecretFileRef("LITELLM_DB_PASSWORD", "DATABASE_PASSWORD_FILE"),
    SecretFileRef("THROUGHPUT_RECORD_TOKEN", "THROUGHPUT_RECORD_TOKEN_FILE"),
)
MODEL_GATEWAY_LANGFUSE_SECRET_FILES: tuple[SecretFileRef, ...] = (
    SecretFileRef("LANGFUSE_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY_FILE"),
    SecretFileRef("LANGFUSE_SECRET_KEY", "LANGFUSE_SECRET_KEY_FILE"),
)
# The admin UI's Google SSO reuses the edge's OAuth client (see ordo.render.litellm_google_sso_env).
MODEL_GATEWAY_GOOGLE_SSO_SECRET_FILES: tuple[SecretFileRef, ...] = (
    SecretFileRef("OAUTH2_PROXY_CLIENT_ID", "GOOGLE_CLIENT_ID_FILE"),
    SecretFileRef("OAUTH2_PROXY_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET_FILE"),
)

# The healthcheck runs as a fresh `docker exec`, outside the entrypoint's environment, so it reads
# the master key from its file itself.
MODEL_GATEWAY_HEALTHCHECK = (
    "python3 -c \"import os, urllib.request; "
    "key = open(os.environ['LITELLM_MASTER_KEY_FILE']).read().strip(); "
    "req = urllib.request.Request('http://localhost:11435/v1/models', "
    "headers={'Authorization': 'Bearer ' + key}); "
    "urllib.request.urlopen(req)\""
)


def _model_gateway(project: str, net: str, langfuse_tracing: bool = False,
                    google_sso_env: dict[str, str] | None = None,
                    available_env=None) -> dict[str, Any]:
    """LiteLLM behind the `local-chat` alias AND the MCP gateway (`/mcp`). The agent gates on
    `model-gateway: service_healthy` (audit G5), so this service MUST render a healthcheck or that
    gate is unsatisfiable and the agent never starts. Probe: GET /v1/models with the master key.

    Mounts the rendered out/model-gateway dir read-only: mcp_servers.yaml (the entrypoint merges it
    into the LiteLLM config) and keys.json (read by model-gateway-keys). Joins the internal MCP
    network so it can reach the mcp-* services. Every secret is a file (MODEL_GATEWAY_SECRET_FILES).

    `langfuse_tracing` (the langfuse plugin is enabled) adds GATEWAY_LANGFUSE_ENV; without it the
    service renders exactly as before and the gateway boots on the template's callbacks alone.

    `google_sso_env` (from ordo.render.litellm_google_sso_env) lets the admin UI's own login be
    the same Google identity as the edge: PROXY_BASE_URL/PROXY_ADMIN_ID are plain values, and the
    edge's OAuth client pair reaches LiteLLM as GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET through the
    files in MODEL_GATEWAY_GOOGLE_SSO_SECRET_FILES. Empty when the edge wiring can't produce a
    PROXY_BASE_URL; the admin/master-key login is unaffected either way (see
    services/model-gateway/README.md)."""
    s = _svc(f"{project}/model-gateway", net=net)
    s["networks"] = [net, _mcp_net(project)]
    s["depends_on"] = _depends_on({"llamacpp": "service_started", "litellm-db": "service_healthy"})
    s["volumes"] = [_MODEL_GATEWAY_CONFIG_BIND]
    s["environment"] = {
        "LITELLM_MODE": "PRODUCTION",   # no load_dotenv(): a stray .env cannot inject credentials
        "LITELLM_LOG": "ERROR",
        # LiteLLM builds DATABASE_URL from these and DATABASE_PASSWORD (a file, see above).
        "DATABASE_HOST": "litellm-db:5432",
        "DATABASE_USERNAME": "litellm",
        "DATABASE_NAME": "litellm",
        "STORE_MODEL_IN_DB": "False",   # config.yaml is the single source of truth for models + MCP
        # uvicorn only trusts X-Forwarded-Proto/Host from loopback by default; caddy connects from
        # a 172.x address on the project network, so without this the /ui slash redirect and the
        # post-login 303 come back http:// on a TLS-only port (reproduced 2026-09-12). No host
        # port is published, so the only peers that can reach this service are project services.
        "FORWARDED_ALLOW_IPS": "*",
    }
    if langfuse_tracing:
        s["environment"].update(GATEWAY_LANGFUSE_ENV)
    if google_sso_env:
        s["environment"].update(google_sso_env)
    _add_derived_env(s, MODEL_GATEWAY_DERIVED_ENV, available_env)
    # Master key (admin API + healthcheck), the DB-credential salt and password, the throughput-record
    # token the dashboard checks, the Langfuse pair only while its callback is on, and the Google
    # client pair only while the admin UI signs in with Google.
    _add_secret_files(s, [*MODEL_GATEWAY_SECRET_FILES,
                          *(MODEL_GATEWAY_LANGFUSE_SECRET_FILES if langfuse_tracing else ()),
                          *(MODEL_GATEWAY_GOOGLE_SSO_SECRET_FILES if google_sso_env else ())])
    s["healthcheck"] = {
        "test": ["CMD-SHELL", MODEL_GATEWAY_HEALTHCHECK],
        "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "60s",
    }
    return s


def _model_gateway_keys(project: str, net: str,
                        key_envs: list[str] | None = None) -> dict[str, Any]:
    """One-shot: provision the per-consumer LiteLLM virtual keys from the rendered keys.json
    (bootstrap_keys.py, idempotent). Same image as the gateway (no second build), runs after the
    gateway is healthy, exits 0 when the desired state holds; `on-failure` retries transient API
    errors. The agent depends on `service_completed_successfully` so Hermes never starts keyless."""
    # The master key to call the admin API, plus every consumer key it provisions (keys.json), all
    # as files: the entrypoint exports the master key, bootstrap_keys.py reads each consumer key with
    # secret_env.read_secret. No derived env: the entrypoint execs this command before it reads any
    # LLAMACPP_* key.
    s = _svc(f"{project}/model-gateway", net=net)
    s["restart"] = "on-failure"
    s["command"] = ["python3", "/app/bootstrap_keys.py"]
    s["volumes"] = [_MODEL_GATEWAY_CONFIG_BIND]
    s["environment"] = {
        "MODEL_GATEWAY_URL": "http://model-gateway:11435",
        "LITELLM_KEYS_SPEC": "/config/keys.json",
    }
    _add_secret_files(s, [SecretFileRef(key, f"{key}_FILE") for key in ["LITELLM_MASTER_KEY", *(key_envs or [])]])
    s["depends_on"] = _depends_on({"model-gateway": "service_healthy"})
    return s


def _dashboard(project: str, net: str, nvidia_gpu: bool,
               dashboard: dict[str, Any] | None = None,
               publish_local_ports: bool = False, available_env=None) -> dict[str, Any]:
    """The control-plane UI service. The dashboard is PLUGGABLE (data-driven, like the agent):
    the selected `dashboard` manifest supplies the image, env, depends_on and healthcheck. When no
    selection is passed (bare/legacy call) it falls back to the V2-native SPA defaults.

    V1's dashboard declares a container HEALTHCHECK on `/api/health`, and the agent gates on
    `dashboard: service_healthy` (audit G5). Keeping a healthcheck on THIS service is REQUIRED or
    that gate is unsatisfiable and the agent never starts — so a manifest that omits one still gets
    the V2-native curl probe as a floor."""
    dashboard = dashboard or {}
    image = dashboard.get("image") or f"{project}/dashboard"
    secrets = dashboard.get("secrets", ())
    # depends_on: manifest may map {peer: condition}; default to start-ordering on ops-controller.
    depends = dashboard.get("depends_on") or {"ops-controller": "service_started"}
    s = _svc(image, net=net)
    dep = _depends_on(depends)
    if dep:
        s["depends_on"] = dep
    env = dashboard.get("environment") or {}
    if env:
        s["environment"] = dict(env)
    # GPU visibility for a dashboard that declares it (`gpu_capabilities`): the NVIDIA runtime only
    # injects nvidia-smi/NVML when the service reserves a GPU with that cap. `count: all` -> every
    # card. NVIDIA hosts only (see _ops_controller). The shipped dashboard declares none: its GPU
    # widgets read ops-controller `GET /gpus` (ordo/render/gpu_live.py).
    gpu_caps = dashboard.get("gpu_capabilities") or []
    if gpu_caps and nvidia_gpu:
        s.update(_capability_gpu_reservation(list(gpu_caps)))
    if dashboard.get("volumes"):
        s["volumes"] = list(dashboard["volumes"])
    s["healthcheck"] = dashboard.get("healthcheck") or {
        "test": ["CMD-SHELL", "curl -sf http://localhost:8080/api/health || exit 1"],
        "interval": "30s", "timeout": "10s", "retries": 3, "start_period": "30s",
    }
    _add_secrets(s, secrets)
    file_secrets = list(dashboard.get("secret_files", ()))
    local_port = dashboard.get("local_port")
    if publish_local_ports and local_port is not None:
        s["ports"] = [local_port.publish()]
        # No SSO edge means no operator identity: the local operator signs in with this secret
        # (dashboard/auth.py). Only rendered with the port, so an edge render never carries it.
        if dashboard.get("local_login_secret"):
            key = dashboard["local_login_secret"]
            file_secrets.append(SecretFileRef(key, f"{key}_FILE"))
    _add_secret_files(s, file_secrets)
    _add_derived_env(s, dashboard.get("derived_env", ()), available_env)
    return s


# The default MCP container probe. A streamable-HTTP MCP endpoint answers a bare GET with an HTTP
# ERROR status (406/405: the protocol wants POST or an SSE Accept header), so ANY HTTP response
# proves the listener is up and serving - that is exactly what urllib raises HTTPError for. A
# URLError or socket error means nothing is listening, and that is the only failure.
MCP_DEFAULT_PROBE = (
    "import urllib.error, urllib.request\n"
    "try: urllib.request.urlopen('http://localhost:{port}{path}', timeout=5)\n"
    "except urllib.error.HTTPError: pass"
)


def default_mcp_healthcheck(port: int, path: str) -> dict[str, Any]:
    """The healthcheck every image-backed MCP service gets unless its manifest overrides it.

    It lives here, not in six copies of a plugin manifest, so the probe is one decision: a manifest
    declares `port` and `path` and the renderer derives the rest. start_period covers a bridged
    image's interpreter + upstream boot (mcp-proxy spawns an npx/uvx server behind it)."""
    return {
        "test": ["CMD", "python3", "-c", MCP_DEFAULT_PROBE.format(port=port, path=path)],
        "interval": "30s",
        "timeout": "10s",
        "retries": 3,
        "start_period": "30s",
    }


def _mcp_service(server: dict[str, Any], *, net: str, mcp_net: str) -> dict[str, Any]:
    """ONE MCP server as a long-lived compose service, from its render record (ordo/render._render_mcp).
    Isolation parity with what the retired Docker gateway spawned (no-new-privileges, 1 CPU / 2 GB,
    init) plus: NO env_file (only the manifest's declared env reaches it), the internal MCP network
    (only model-gateway can call it), `stack` network only when it must reach another service, and
    labels ops-api uses to list MCP services. LiteLLM dials http://mcp-<id>:<port><path>.
    The healthcheck defaults to default_mcp_healthcheck(port, path); a manifest `healthcheck:` is an
    override for the images that cannot run it (searxng-mcp ships node, not python3)."""
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
        "healthcheck": (dict(server["healthcheck"])
                        or default_mcp_healthcheck(server["port"], server["path"])),
    }
    if server["env"]:
        s["environment"] = dict(server["env"])
    if server["command"]:
        s["command"] = list(server["command"])
    if server["volumes"]:
        s["volumes"] = list(server["volumes"])
    if server["depends_on"]:
        s["depends_on"] = list(server["depends_on"])
    _add_secret_files(s, server.get("secret_files", ()))
    return s


def _apply_agent_runtime(svc: dict[str, Any], *, user: str | None, group_add: list[str] | None,
                         volumes: list[str] | None,
                         environment: dict[str, str] | None,
                         secret_files: list[SecretFileRef] | None,
                         depends_on: dict[str, str] | None,
                         healthcheck: dict[str, Any] | None) -> None:
    """Layer the agent manifest's runtime wiring onto the base agent service (in place). File
    secrets render as read-only mounts of the materialized out/secrets/* files (ordo/render/secret_files.py).
    depends_on with conditions overrides the plain start-order list so V1's service_healthy gates
    are mirrored."""
    if user:
        svc["user"] = user
    if group_add:
        svc["group_add"] = list(group_add)
    if volumes:
        svc["volumes"] = list(volumes)
    if environment:
        svc["environment"] = dict(environment)
    _add_secret_files(svc, secret_files or ())
    dep = _depends_on(depends_on)
    if dep:
        svc["depends_on"] = dep  # long-form conditions replace the base plain list
    if healthcheck:
        svc["healthcheck"] = dict(healthcheck)


def _plugin_service(ps: PluginService, plugin: Plugin, *, net: str,
                    nvidia_gpu: bool, primary_uuid: str | None, secondary_uuid: str | None,
                    project: str, publish_local_ports: bool = False,
                    available_env=None, edge_ports: list[int] | None = None) -> dict[str, Any]:
    """Render ONE compose service from a plugin's declared PluginService — data-driven, so
    adding a service is a manifest edit, not a code change here. `${...}` / `./...` refs and
    named volumes pass straight through to compose (project-scoped, no live-stack collision).

    `edge_ports` are the enabled UIs' declared `edge_site` ports; the edge listener publishes them."""
    s: dict[str, Any] = {"image": ps.image, "restart": ps.restart or "unless-stopped", "networks": [net]}
    if ps.network_mode:
        # compose forbids networks: alongside network_mode: — the service lives in the
        # target's namespace (e.g. the tailnet-name sidecars inside Caddy's netns).
        s.pop("networks")
        s["network_mode"] = ps.network_mode
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
    elif (ps.gpu or ps.gpu_pin) and nvidia_gpu:
        # a GPU service on a machine whose primary uuid didn't resolve (CI/mock) — fall back to the
        # all-GPU reservation so the shape is still valid; the uuid pin is added when detect() has it.
        s.update(_GPU_RESERVATION)
    if env:
        s["environment"] = env
    _add_secrets(s, ps.secrets)
    _add_derived_env(s, ps.derived_env, available_env)
    if ps.command:
        s["command"] = list(ps.command)
    if ps.volumes:
        s["volumes"] = list(ps.volumes)
    if ps.healthcheck:
        s["healthcheck"] = dict(ps.healthcheck)
    dep = _depends_on(ps.depends_on)
    if ps.network_mode.startswith("service:"):
        # This service shares another service's network namespace (the tailnet-name sidecars and
        # hermes-dashboard join caddy's netns; `tailscale serve`/loopback binds can only target
        # 127.0.0.1). Recreating or restarting the OWNER destroys that shared sandbox and orphans
        # every member: its tailscale node goes offline / its loopback upstream is unreachable, yet
        # the container stays up and can still report healthy. So the member's lifecycle MUST be
        # coupled to the owner's — `depends_on.<owner>.restart: true` (compose spec, Compose v2.17+)
        # makes `docker compose up -d` and `docker compose restart <owner>` bring the member with the
        # owner atomically, replacing the manual `docker restart ordo-tailnet-*` after every caddy
        # recreate. (A BARE `docker restart <owner>` bypasses compose and still won't cascade. The
        # `--no-deps` paths, `ordo up/recreate` and ops-controller's lifecycle verbs, name the
        # members explicitly through `stack.lifecycle_group`.) depends_on must be all-or-nothing
        # long form, so peers keep the default service_started condition and only the owner
        # carries restart.
        owner = ps.network_mode.split("service:", 1)[1]
        # dict(dep) keeps any declared `service_healthy` conditions; a plain list becomes the
        # default service_started, because depends_on must be all-or-nothing long form here.
        dep_map: dict[str, Any] = (dict(dep) if isinstance(dep, dict)
                                   else {peer: {"condition": "service_started"} for peer in (dep or [])})
        dep_map[owner] = {"condition": "service_started", "restart": True}
        s["depends_on"] = dep_map
    elif dep:
        s["depends_on"] = dep
    if ps.edge_listener is not None:  # the edge (Caddy): its front door plus every enabled UI's port
        s["ports"] = ps.edge_listener.publish(list(edge_ports or []))
    if publish_local_ports and ps.local_port is not None:  # loopback-only, and only without the edge
        s["ports"] = s.get("ports", []) + [ps.local_port.publish()]
    if ps.shm_size:  # bump /dev/shm past docker's 64MB default (Electron/Selkies streaming needs it)
        s["shm_size"] = ps.shm_size
    if ps.entrypoint:  # REPLACES the image's baked ENTRYPOINT (exec form - no shell splitting)
        s["entrypoint"] = list(ps.entrypoint)
    if ps.security_opt:
        s["security_opt"] = list(ps.security_opt)
    if ps.ulimits:
        s["ulimits"] = dict(ps.ulimits)
    if ps.resources:
        # MERGE, never replace: a GPU service already carries deploy.resources.reservations from
        # the pin above, and overwriting `deploy` here would silently drop its device reservation.
        resources = s.setdefault("deploy", {}).setdefault("resources", {})
        resources["limits"] = dict(ps.resources)
    _add_secret_files(s, ps.secret_files)
    return s


def _gpu_gate(ps: PluginService, plugin: Plugin, claim: Any, *, net: str, upstream_net: str,
              project: str) -> tuple[str, dict[str, Any]]:
    """Render the admission gate that fronts a `gpu_arbitration.enforcement: gate` service.

    Derived entirely from the declaration — one generic image, no per-service code. The gate
    listens on the upstream's port under the name `<service>-gate`, so pointing a consumer at it
    is a hostname change and nothing else. It reserves NO GPU: it is an HTTP proxy that acquires
    residency from ops-controller before letting a submission through, and it is a CLIENT of the
    arbiter — it never starts or stops another container.

    It joins two networks: `net`, where its callers are, and `upstream_net`, the upstream's
    private network (gated_upstream_net), which no other service joins.
    """
    g = ps.gpu_arbitration.gate
    name = gpu.gate_service_name(ps.name)
    s: dict[str, Any] = {
        "image": f"{project}/gpu-gate",
        "restart": "unless-stopped",
        "networks": [net, upstream_net],
        "depends_on": [ps.name],
        "environment": {
            "GATE_UPSTREAM": f"http://{ps.name}:{g.upstream_port}",
            "GATE_UPSTREAM_SERVICE": ps.name,
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
    # It takes GPU leases from ops-controller; gate.py reads the token with secret_env.read_secret.
    _add_secret_files(s, [SecretFileRef("OPS_CONTROLLER_TOKEN", "OPS_CONTROLLER_TOKEN_FILE")])
    return name, s


def render_compose(*, nvidia_gpu: bool, llamacpp_backend: LlamaCppBackend,
                   compose_profiles: list[str], agent: str = "hermes",
                   project: str = "ordo",
                   agent_image: str | None = None,
                   agent_command: list[str] | None = None,
                   agent_user: str | None = None,
                   agent_group_add: list[str] | None = None,
                   agent_volumes: list[str] | None = None,
                   agent_environment: dict[str, str] | None = None,
                   agent_secret_files: list[SecretFileRef] | None = None,
                   agent_secrets: list[str] | None = None,
                   agent_derived_env: list[str] | None = None,
                   litellm_key_envs: list[str] | None = None,
                   agent_depends_on: dict[str, str] | None = None,
                   agent_healthcheck: dict[str, Any] | None = None,
                   dashboard: dict[str, Any] | None = None,
                   llamacpp_image: str | None = None,
                   plugin_services: list[tuple[Plugin, PluginService]] | None = None,
                   primary_gpu_uuid: str | None = None,
                   secondary_gpu_uuid: str | None = None,
                   gpu_claims: dict[str, Any] | None = None,
                   mcp_servers: list[dict[str, Any]] | None = None,
                   langfuse_tracing: bool = False,
                   litellm_google_sso_env: dict[str, str] | None = None,
                   publish_local_ports: bool = False,
                   available_env: frozenset[str] | None = None,
                   edge_ports: list[int] | None = None,
                   model_gateway_config_digest: str | None = None) -> dict[str, Any]:
    """`available_env` is the set of keys in the rendered .env (RenderedConfig.env): a declared
    derived key renders only when the render produced it. None emits every declared key.

    `edge_ports` are the enabled UIs' declared `edge_site` ports (RenderedConfig.edge_sites),
    published on the one service that declares `edge_listener`.

    `model_gateway_config_digest` (`rendered_config_digest` of out/model-gateway/) is stamped on
    MODEL_GATEWAY_CONFIG_READERS; None leaves the label off."""
    net = f"{project}-net"
    # the agent is swappable (Hermes is the default); a registry manifest may pin any image,
    # else fall back to the <project>/agent-<id> convention (render tags it, see ordo/host/images.py).
    agent_img = agent_image or f"{project}/agent-{agent}"
    # The llama.cpp build is the host's backend (ordo/render/llamacpp_backend.py: CPU, CUDA, ROCm or
    # Vulkan upstream server image) unless the chosen model pins its own build (e.g. the patched
    # Qwen3.6/3.8 image) via its catalog `backend_image`. render resolves which and passes it in
    # as llamacpp_image; the backend alone decides how the service reaches the GPU.
    llamacpp_img = llamacpp_image or llamacpp_backend.image
    uses_cuda = llamacpp_backend.name == "cuda"
    llamacpp = _svc(llamacpp_img, net=net, gpu=uses_cuda)
    # always-on Prometheus metrics endpoint (the monitoring plugin's prometheus scrapes it).
    llamacpp["command"] = [LLAMACPP_METRICS_ARG]
    # Pin the compute service to the PRIMARY card by uuid (V1 does this in gpu-assignments.yml).
    # Without the CUDA_VISIBLE_DEVICES pin, on a dual-GPU WSL2 box `count: all` lets llama.cpp see
    # the 1070 too — a failure that only surfaces against real dual-GPU hardware. The
    # `.env` still carries no pin; this is a compose-level env override on the service.
    if uses_cuda and primary_gpu_uuid:
        llamacpp["deploy"] = _gpu_pinned_reservation(primary_gpu_uuid)["deploy"]
        llamacpp["environment"] = _pin_env(primary_gpu_uuid)
    # ROCm / Vulkan: compose has no device driver for these GPUs, so the device nodes are passed
    # through instead (/dev/kfd + /dev/dri for ROCm, /dev/dri for Vulkan). EXPERIMENTAL: rendered
    # and `docker compose config`-validated, not yet run on real AMD/Intel hardware.
    _add_derived_env(llamacpp, LLAMACPP_DERIVED_ENV, available_env)
    if llamacpp_backend.devices:
        llamacpp["devices"] = list(llamacpp_backend.devices)
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
        # brain (#143) and comfyui-storage (#156). `ordo fetch` (and `ordo up`, for any
        # file missing) downloads straight into this volume through a helper container
        # (ordo/host/fetch.py); models/gguf is retired from every hot path.
        "models-gguf:/models:ro",
        "${BASE_PATH:?BASE_PATH must be set (non-empty)}/scripts/llamacpp:/llamacpp-scripts:ro",
    ]
    # model-gateway is the V1 custom-built LiteLLM config wrapper (+ the MCP gateway since 2026-09);
    # a first-party BUILDABLE image (build context services/model-gateway) so preflight reports
    # 'build first' not 'Docker will pull'. The V2-native ops-controller + dashboard remain the new control plane.
    svcs: dict[str, Any] = {
        "llamacpp": llamacpp,
        "litellm-db": _litellm_db(net),
        # LITELLM_MASTER_KEY + LITELLM_SALT_KEY + THROUGHPUT_RECORD_TOKEN are secrets (secrets.env).
        "model-gateway": _model_gateway(project, net, langfuse_tracing=langfuse_tracing,
                                         google_sso_env=litellm_google_sso_env,
                                         available_env=available_env),
        "model-gateway-keys": _model_gateway_keys(project, net, litellm_key_envs),
        "ops-controller": _ops_controller(project, net, nvidia_gpu),
        # The dashboard is pluggable (data-driven): the selected manifest supplies image/env/
        # depends/healthcheck. It has no backend service of its own — it calls ops-controller.
        "dashboard": _dashboard(project, net, nvidia_gpu, dashboard, publish_local_ports,
                                available_env=available_env),
        # Env secrets from the agent manifest's `secrets:`; Discord/backup tokens are file secrets.
        "agent": _svc(agent_img, net=net,
                      depends=["model-gateway", "model-gateway-keys", "ops-controller"]),
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
        svcs["agent"], user=agent_user, group_add=agent_group_add, volumes=agent_volumes,
        environment=agent_environment, secret_files=agent_secret_files,
        depends_on=agent_depends_on, healthcheck=agent_healthcheck)
    _add_secrets(svcs["agent"], agent_secrets or ())  # after the manifest env, which would replace it
    _add_derived_env(svcs["agent"], agent_derived_env or (), available_env)
    # optional plugin services, built from the resolved manifests (no hardcoded if-blocks).
    # render() only passes services whose plugin is enabled, so profile-gating already happened;
    # the per-service `profiles:` keeps them dormant until `--profile <p>` is used too.
    claims = gpu_claims or {}
    gated_nets: list[str] = []
    listeners = [f"{plugin.id}/{ps.name}" for plugin, ps in (plugin_services or []) if ps.edge_listener]
    if len(listeners) > 1:
        raise ValueError(f"more than one edge_listener is enabled ({', '.join(listeners)}); the UI ports "
                         "can be published on one service only")
    for plugin, ps in (plugin_services or []):
        svcs[ps.name] = _plugin_service(ps, plugin, net=net,
                                        nvidia_gpu=nvidia_gpu, primary_uuid=primary_gpu_uuid,
                                        secondary_uuid=secondary_gpu_uuid,
                                        project=project, publish_local_ports=publish_local_ports,
                                        available_env=available_env, edge_ports=edge_ports)
        # A service whose GPU use is gate-enforced gets its gate rendered WITH it, from the same
        # declaration. Not opt-in and not a separate manifest entry: the two cannot disagree, and
        # an enabled gated service can never come up without the thing that arbitrates it.
        arb = ps.gpu_arbitration
        if arb is not None and arb.enforcement == "gate" and ps.name in claims:
            if ps.network_mode:
                # Sharing another container's namespace would make the upstream reachable by
                # every peer of that container, voiding the isolation below without a trace.
                raise ValueError(
                    f"{plugin.id}/{ps.name} is gpu_arbitration.enforcement: gate, so it must sit on "
                    f"its own private network; network_mode {ps.network_mode!r} cannot be combined "
                    f"with that")
            upstream_net = gated_upstream_net(project, ps.name)
            # The upstream leaves the stack network entirely: its only peer is its gate.
            svcs[ps.name]["networks"] = [upstream_net]
            gated_nets.append(upstream_net)
            gate_name, gate_svc = _gpu_gate(ps, plugin, claims[ps.name], net=net,
                                            upstream_net=upstream_net, project=project)
            svcs[gate_name] = gate_svc

    # MCP servers: one compose service per image-backed record; hosted (url-only) servers render
    # nothing here (LiteLLM dials them directly).
    for server in (mcp_servers or []):
        if not server["hosted"]:
            svcs[server["service"]] = _mcp_service(server, net=net, mcp_net=_mcp_net(project))

    if model_gateway_config_digest:
        for reader in MODEL_GATEWAY_CONFIG_READERS:
            svcs[reader].setdefault("labels", {})[RENDERED_CONFIG_LABEL] = model_gateway_config_digest

    out: dict[str, Any] = {
        "name": project,
        "services": svcs,
        "networks": {net: {"name": net}, _mcp_net(project): {"name": _mcp_net(project), "internal": True},
                     **{n: {"name": n} for n in gated_nets}},
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
