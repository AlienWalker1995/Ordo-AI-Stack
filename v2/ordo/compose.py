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


def _svc(image: str, *, net: str, env_file: str | None = None, gpu: bool = False,
         profiles: list[str] | None = None, depends: list[str] | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"image": image, "restart": "unless-stopped", "networks": [net]}
    if env_file:
        s["env_file"] = [env_file]
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
    s = _svc(f"{project}/ops-controller:latest", net=net, env_file=env_file)
    s["volumes"] = [
        "/var/run/docker.sock:/var/run/docker.sock",  # broker start/stop (guard-scoped)
        "./:/config",                                 # ordo.yaml + rendered out/ (single write path)
    ]
    s["environment"] = {"ORDO_PROJECT": project}
    # --source/--catalog are global (pre-subcommand) flags; --project/--out belong to `serve`.
    s["command"] = ["--source", "/config/ordo.yaml", "serve", "--project", project, "--out", "/config/out"]
    return s


def _plugin_service(ps: "PluginService", plugin: "Plugin", *, net: str, env_file: str,
                    has_gpu: bool, secondary_uuid: str | None,
                    project: str) -> dict[str, Any]:
    """Render ONE compose service from a plugin's declared PluginService — data-driven, so
    adding a service is a manifest edit, not a code change here. `${...}` / `./...` refs and
    named volumes pass straight through to compose (project-scoped, no live-stack collision)."""
    s: dict[str, Any] = {"image": ps.image, "restart": "unless-stopped", "networks": [net]}
    if env_file:
        s["env_file"] = [env_file]
    if plugin.compose_profile:
        s["profiles"] = [plugin.compose_profile]
    env = dict(ps.env)
    # GPU wiring: a `secondary` pin lands on the non-primary card (voice → Pascal 1070) via BOTH
    # CUDA_VISIBLE_DEVICES (the only thing WSL2 honors) AND a device_ids reservation.
    if ps.gpu_pin == "secondary" and secondary_uuid:
        env["CUDA_VISIBLE_DEVICES"] = secondary_uuid
        env["NVIDIA_VISIBLE_DEVICES"] = secondary_uuid
        s.update(_gpu_pinned_reservation(secondary_uuid))
    elif ps.gpu and has_gpu:
        s.update(_GPU_RESERVATION)
    if env:
        s["environment"] = env
    if ps.command:
        s["command"] = list(ps.command)
    if ps.volumes:
        s["volumes"] = list(ps.volumes)
    if ps.healthcheck:
        s["healthcheck"] = dict(ps.healthcheck)
    if ps.depends_on:
        s["depends_on"] = list(ps.depends_on)
    return s


def render_compose(*, has_gpu: bool, compose_profiles: list[str], agent: str = "hermes",
                   project: str = "ordo-v2", env_file: str = ".env",
                   agent_image: str | None = None,
                   llamacpp_image: str | None = None,
                   plugin_services: "list[tuple[Plugin, PluginService]] | None" = None,
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
    svcs: dict[str, Any] = {
        "llamacpp": llamacpp,
        "model-gateway": _svc("ghcr.io/berriai/litellm:main", net=net, env_file=env_file,
                              depends=["llamacpp"]),
        "mcp-gateway": _svc("docker/mcp-gateway:latest", net=net, env_file=env_file),
        "ops-controller": _ops_controller(project, net, env_file),
        "dashboard": _svc(f"{project}/dashboard:latest", net=net, env_file=env_file,
                          depends=["ops-controller"]),
        "agent": _svc(agent_img, net=net, env_file=env_file,
                      depends=["model-gateway", "mcp-gateway", "ops-controller"]),
    }
    # optional plugin services, built from the resolved manifests (no hardcoded if-blocks).
    # render() only passes services whose plugin is enabled, so profile-gating already happened;
    # the per-service `profiles:` keeps them dormant until `--profile <p>` is used too.
    for plugin, ps in (plugin_services or []):
        svcs[ps.name] = _plugin_service(ps, plugin, net=net, env_file=env_file,
                                        has_gpu=has_gpu, secondary_uuid=secondary_gpu_uuid,
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
