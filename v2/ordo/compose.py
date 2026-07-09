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

from typing import Any

# The mandatory 6-service core (from the architecture decisions). Caddy/oauth2-proxy is an
# OPTIONAL remote-access plugin, so it's not here — a local floor install is localhost-only.
_CORE = ["llamacpp", "model-gateway", "mcp-gateway", "ops-controller", "dashboard"]

_GPU_RESERVATION = {
    "deploy": {"resources": {"reservations": {"devices": [
        {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]}}}
}


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


def render_compose(*, has_gpu: bool, compose_profiles: list[str], agent: str = "hermes",
                   project: str = "ordo-v2", env_file: str = ".env",
                   agent_image: str | None = None) -> dict[str, Any]:
    net = f"{project}-net"
    # the agent is swappable (Hermes is the default); a registry manifest may pin any image,
    # else fall back to the <project>/agent-<id>:latest convention.
    agent_img = agent_image or f"{project}/agent-{agent}:latest"
    svcs: dict[str, Any] = {
        "llamacpp": _svc("ghcr.io/ggml-org/llama.cpp:server", net=net, env_file=env_file, gpu=has_gpu),
        "model-gateway": _svc("ghcr.io/berriai/litellm:main", net=net, env_file=env_file,
                              depends=["llamacpp"]),
        "mcp-gateway": _svc("docker/mcp-gateway:latest", net=net, env_file=env_file),
        "ops-controller": _ops_controller(project, net, env_file),
        "dashboard": _svc(f"{project}/dashboard:latest", net=net, env_file=env_file,
                          depends=["ops-controller"]),
        "agent": _svc(agent_img, net=net, env_file=env_file,
                      depends=["model-gateway", "mcp-gateway", "ops-controller"]),
    }
    # optional plugin services appear only behind their profile
    if "media" in compose_profiles:
        svcs["comfyui"] = _svc(f"{project}/comfyui:latest", net=net, env_file=env_file,
                               gpu=has_gpu, profiles=["media"])
    if "voice" in compose_profiles:
        svcs["voice"] = _svc(f"{project}/voice:latest", net=net, env_file=env_file,
                             gpu=has_gpu, profiles=["voice"])

    return {"name": project, "services": svcs, "networks": {net: {"name": net}}}


def core_services() -> list[str]:
    return list(_CORE)
