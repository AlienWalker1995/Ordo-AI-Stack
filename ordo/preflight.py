"""Preflight — a read-only GO / NO-GO readiness check before bringing a stack up.

"Is it safe to deploy this config yet?" should be a command, not a vibe. `ordo preflight`
renders the target config and checks every gate we can verify WITHOUT starting anything:

  - config renders and the one ctx value is consistent across all consumers (the drift gate),
  - the active model is sha256-pinned (corrupt-weights gate) and MCP images are digest-pinned,
  - if a GPU is expected for the enabled media/voice plugins, one is actually present,
  - parity vs a reference .env (merge-gate (a)) when a --ref is given,
  - every image the rendered compose needs is available: project images (ordo/*) must be
    built locally (blocking); upstream images (llama.cpp, litellm, …) may be absent — Docker
    pulls them (a note, not a blocker).

Host checks (`host_checks`) answer the other half, "can THIS machine run it": Docker reachable,
Compose v2, the NVIDIA runtime when a GPU is reserved, disk for every model file still to fetch,
free host ports, and no blank required secret. `ordo up` runs them before it starts anything.

Blocking checks failing = NO-GO. Non-blocking = a warning you can proceed past knowingly.
Every verdict is pure logic over injected facts; the I/O that gathers the facts (docker, sockets,
disk) is the `gather_host_facts` section at the bottom, and the CLI wires the real `docker images`.
"""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from . import buildspec, fetch, parity, served_models
from .agents import AgentRegistry
from .catalog import Catalog
from .config import Source
from .dashboards import DashboardRegistry
from .plugins import PluginRegistry
from .render import (
    DEFAULT_AGENTS_DIR,
    DEFAULT_DASHBOARDS_DIR,
    render,
)

# ${VAR}, ${VAR:-default} or ${VAR:?message}: the compose interpolation a rendered value may carry
# (e.g. `${COMFYUI_IMAGE:-yanwk/comfyui-boot@sha256:…}`, `${CADDY_BIND:?…}:443:443`). Resolved
# against the rendered .env (with the `:-default` fallback) so a check compares the ACTUAL value.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")


def _expand(value: str, env: dict[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        val = env.get(m.group(1))
        if val not in (None, ""):
            return val
        is_default = (m.group(2) or "").endswith("-")
        return (m.group(3) or "") if is_default else ""
    return _VAR_RE.sub(sub, value)


@dataclasses.dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def required_images(rc, project: str = "ordo", image_tags: dict[str, str] | None = None) -> list[str]:
    """The exact images the rendered compose will need (core + agent + enabled plugins), with
    ${VAR:-default} refs expanded against the rendered .env so presence-matching is accurate.
    `image_tags` is the `ordo build` record the render pins first-party images to."""
    c = rc.compose_dict(project=project, image_tags=image_tags)
    return sorted({_expand(svc["image"], rc.env) for svc in c["services"].values()})


def secret_checks(needed: Iterable[str], optional: Iterable[str], secrets_path: str) -> list[Check]:
    """Blank or absent secrets among `needed`: a REQUIRED one blocks, an optional one is a note.

    Names only, never values. `optional` is what the manifests declare the stack runs without
    (`optional_secrets:`, e.g. HF_TOKEN), so a blank one of those never stops a bring-up."""
    optional_set = set(optional)
    if not Path(secrets_path).exists():
        return [Check("required secrets set", False,
                      f"{secrets_path} is missing: run `ordo init` (it writes one) or copy secrets.env.example")]
    present = {k for k, v in parity.load_env(secrets_path).items() if v}
    blank = [k for k in dict.fromkeys(needed) if k not in present]
    blank_required = [k for k in blank if k not in optional_set]
    blank_optional = [k for k in blank if k in optional_set]
    checks = [Check("required secrets set", not blank_required,
                    "all set" if not blank_required
                    else f"blank in {secrets_path}: {', '.join(blank_required)} (fill them in, then re-run)")]
    if blank_optional:
        checks.append(Check("optional secrets", False,
                            f"blank (features that need them stay limited): {', '.join(blank_optional)}",
                            blocking=False))
    return checks


def run(
    source: Source, catalog: Catalog, registry: PluginRegistry, *,
    ref_env: str | None = None,
    images_present: set[str] | None = None,
    secrets_env: str | None = None,
    project: str = "ordo",
    image_tags: dict[str, str] | None = None,
    agents: AgentRegistry | None = None,
    dashboards: DashboardRegistry | None = None,
) -> tuple[bool, list[Check]]:
    # Load the agent/dashboard registries the SAME way render() does (from the co-located manifest
    # dirs) so the image→context resolver sees exactly what was rendered.
    if agents is None:
        agents = AgentRegistry.load(DEFAULT_AGENTS_DIR)
    if dashboards is None:
        dashboards = DashboardRegistry.load(DEFAULT_DASHBOARDS_DIR)
    rc = render(source, catalog, registry, agents=agents, dashboards=dashboards)
    # Single image→context resolver over substrate + manifests. A project image resolves to a build
    # context (or buildspec.EXTERNAL for an out-of-band image); an upstream pull image resolves to
    # None. This REPLACES the old `_is_buildable` substring special-case + the hardcoded llamacpp hint.
    resolve_ctx = buildspec.context_resolver(registry, agents, dashboards, project=project)

    def _is_buildable(image: str) -> bool:
        return resolve_ctx(image) is not None

    checks: list[Check] = []

    # 1. drift gate — one ctx value everywhere
    derived = rc.manifest()["derived"]
    consistent = len({str(v) for v in derived.values()}) == 1
    checks.append(Check("config renders + ctx consistent across .env/hermes/model-gateway",
                        consistent, f"ctx={rc.ctx_size:,}" if consistent else str(derived)))

    # 2. corrupt-weights gate — the chosen model is checksum-pinned
    checks.append(Check(f"active model '{rc.model.id}' is sha256-pinned",
                        rc.model.sha256 is not None,
                        "pinned" if rc.model.sha256 else "NO sha256 — download refuses unless --allow-unverified",
                        blocking=False))

    # 3. MCP images digest-pinned (drift/leak gate) — warn per unpinned PUBLIC server. Locally-built
    # project images (ordo/*) are pinned by build context, not a registry digest, so exempt. A hosted
    # server (a `url:` with no container) has no image to pin, so it is skipped too.
    unpinned_mcp = [s["id"] for s in rc.mcp_servers
                    if not s.get("hosted")
                    and not str(s.get("image", "")).startswith(f"{project}/")
                    and ("@sha256:" not in str(s.get("image", ""))
                         or len(set(str(s["image"]).split("@sha256:")[-1])) <= 1)]
    checks.append(Check("all enabled MCP images digest-pinned", not unpinned_mcp,
                        "all pinned" if not unpinned_mcp else f"placeholder/unpinned: {', '.join(unpinned_mcp)}",
                        blocking=False))

    # 3b. Plugin SERVICE images pinned (pin-don't-float gate; audit P1-5) — warn per service whose
    # image is a rolling/floating tag. Accepted as pinned: a registry digest (@sha256:), a
    # version-looking tag (:v1.2 / :2.28.3 / :v7.15.3-alpine), or a locally-built project image
    # (pinned by build context). ${VAR:-default} refs are judged by their default.
    def _float(img: str) -> bool:
        img = str(img)
        if img.startswith("${") and ":-" in img:            # unwrap ${VAR:-default}
            img = img.split(":-", 1)[1].rstrip("}")
        if _is_buildable(img) or "@sha256:" in img:
            return False
        tag = img.rsplit(":", 1)[-1] if ":" in img.rsplit("/", 1)[-1] else "latest"
        return not re.match(r"^v?\d+(\.\d+)+", tag)          # not a version tag => rolling
    floating = sorted({name for name, svc in rc.compose_dict(project=project)["services"].items()
                       if _float(svc.get("image", ""))})
    checks.append(Check("service images pinned (no rolling tags)", not floating,
                        "all pinned" if not floating else f"rolling/floating: {', '.join(floating)}",
                        blocking=False))

    # 4. GPU present if media/voice plugins are enabled
    gpu_plugins = [p for p in rc.plugins_enabled if p in ("comfyui", "song-gen", "voice")]
    gpu_ok = rc.hardware.has_gpu or not gpu_plugins
    checks.append(Check("GPU present for enabled media/voice plugins", gpu_ok,
                        "no GPU-only plugins" if not gpu_plugins else
                        (f"GPU present ({rc.hardware.primary_vram_gb:.0f}GB)" if gpu_ok
                         else f"media plugins {gpu_plugins} need a GPU but none detected")))

    # 5. merge-gate (a): parity vs the live .env (read-only)
    if ref_env:
        ok, mism, compared = parity.report(rc.env, ref_env)
        checks.append(Check(f"parity vs live .env ({ref_env})", ok,
                            f"{len(compared)} keys compared, 0 mismatch" if ok
                            else f"{len(mism)} mismatch: {', '.join(sorted(mism))}"))

    # 6. images available — project images must be built (blocking); upstream may be pulled (note)
    if images_present is not None:
        needed = required_images(rc, project, image_tags)
        proj_missing = [i for i in needed if _is_buildable(i) and i not in images_present]
        upstream_missing = [i for i in needed
                            if not _is_buildable(i) and i not in images_present]
        detail = "all built"
        if proj_missing:
            # Generic build-from hint derived from the single resolver — every project image gets
            # a context pointer (no per-image special-case). An out-of-band image (EXTERNAL) has no
            # in-repo context, so it's shown bare.
            hints = []
            for i in proj_missing:
                ctx = resolve_ctx(i)
                hints.append(f"{i} (build from {ctx})" if ctx and ctx != buildspec.EXTERNAL else i)
            detail = f"build first (`ordo build --all`, or `ordo up` builds them): {', '.join(hints)}"
        checks.append(Check("project images built locally", not proj_missing, detail))
        if upstream_missing:
            checks.append(Check("upstream images cached", False,
                                f"Docker will pull: {', '.join(upstream_missing)}", blocking=False))

    # 7. secrets: when a secrets.env is given, a blank REQUIRED key blocks (the service that reads
    # it would crash-loop); a blank optional one (declared `optional_secrets:`) is only a note.
    if secrets_env is not None:
        checks += secret_checks(rc.required_secrets, rc.optional_secrets, secrets_env)

    go = all(c.ok for c in checks if c.blocking)
    return go, checks


# ── Host checks: can THIS machine run the rendered stack? ─────────────────────
@dataclasses.dataclass(frozen=True)
class HostFacts:
    """What the host looks like, gathered by `gather_host_facts` (or built by a test)."""
    docker_error: str | None                  # None: the daemon answered `docker info`
    compose_version: str | None               # `docker compose version --short`; None when absent
    runtimes: frozenset[str]                  # container runtimes the daemon has registered
    busy_ports: frozenset[tuple[str, int]]    # published (address, port) pairs another process holds
    disk_path: str                            # where the model's volume lands (or the best proxy)
    disk_free_gb: float | None
    # The file names already in the models volume (empty when it does not exist yet); None when it
    # could not be listed, so every model file counts as still to fetch.
    volume_files: frozenset[str] | None = None


@dataclasses.dataclass(frozen=True)
class ModelFile:
    """A weights file a service loads from the models volume, and its size."""
    file: str
    gb: float


def model_files(services: dict, env: Mapping[str, str], catalog: Catalog) -> list[ModelFile]:
    """Every file `services` load from the models volume (chat model, projector, CPU fallback,
    embedder), sized from its catalog entry. An entry with no pinned size_bytes falls back to its
    vram_gb / ram_gb estimate; a file with no catalog entry counts 0 (`ordo up` refuses it anyway)."""
    found = []
    for need in served_models.model_files({"services": services}, env):
        entry = catalog.by_file(need.file)
        if entry is None:
            gb = 0.0
        elif entry.size_bytes:
            gb = entry.size_bytes / 1024 ** 3
        else:
            gb = entry.vram_gb or entry.ram_gb
        found.append(ModelFile(need.file, gb))
    return found


def published_ports(services: dict, env: dict[str, str]) -> list[tuple[str, int]]:
    """Every (address, host port) the services publish, with `${VAR}` resolved from the .env.

    Only `address:host:container` and `host:container` publish a fixed host port; a bare container
    port gets a random one, which cannot collide."""
    found: list[tuple[str, int]] = []
    for spec in services.values():
        for raw in (spec or {}).get("ports") or []:
            parts = _expand(str(raw), env).split("/")[0].split(":")
            if len(parts) == 3:
                address, host_port = parts[0] or "0.0.0.0", parts[1]
            elif len(parts) == 2:
                address, host_port = "0.0.0.0", parts[0]
            else:
                continue
            if host_port.isdigit():
                found.append((address, int(host_port)))
    return found


def nvidia_services(services: dict) -> list[str]:
    """Services whose compose reserves an NVIDIA device (they need the NVIDIA container runtime)."""
    names = []
    for name, spec in services.items():
        resources = ((spec or {}).get("deploy") or {}).get("resources") or {}
        devices = (resources.get("reservations") or {}).get("devices") or []
        if any(str(device.get("driver", "")) == "nvidia" for device in devices):
            names.append(name)
    return sorted(names)


def secret_refs(services: dict, secret_keys: Iterable[str]) -> list[str]:
    """The secret keys the services reference as `${KEY}` anywhere in their definition."""
    keys = set(secret_keys)
    text = json.dumps(services)
    return sorted({m.group(1) for m in _VAR_RE.finditer(text) if m.group(1) in keys})


def _compose_major(version: str) -> int:
    match = re.match(r"v?(\d+)", version.strip())
    return int(match.group(1)) if match else 0


def host_checks(services: dict, env: dict[str, str], facts: HostFacts, *, secret_keys: Iterable[str],
                optional_secrets: Iterable[str], secrets_path: str | None,
                model_files: Sequence[ModelFile]) -> list[Check]:
    """One Check per host requirement of the services about to start. Each failure is one line
    that says what to do."""
    checks = [Check("docker daemon reachable", facts.docker_error is None,
                    "ok" if facts.docker_error is None
                    else f"start Docker (Docker Desktop, or `sudo systemctl start docker`): {facts.docker_error}")]

    compose_ok = facts.compose_version is not None and _compose_major(facts.compose_version) >= 2
    checks.append(Check("docker compose v2 present", compose_ok,
                        f"v{facts.compose_version}" if compose_ok else
                        f"install the Docker Compose v2 plugin (`docker compose`); found "
                        f"{facts.compose_version or 'none'}"))

    gpu_services = nvidia_services(services)
    if gpu_services and facts.docker_error is None:
        has_runtime = "nvidia" in facts.runtimes
        checks.append(Check("NVIDIA container runtime present", has_runtime,
                            "registered" if has_runtime else
                            f"{', '.join(gpu_services)} reserve an NVIDIA GPU: install the NVIDIA Container "
                            f"Toolkit and run `sudo nvidia-ctk runtime configure --runtime=docker`, then "
                            f"restart Docker"))

    if model_files and facts.disk_free_gb is not None:
        present = facts.volume_files
        to_fetch = [f for f in model_files if present is None or f.file not in present]
        need_gb = sum(f.gb for f in to_fetch)
        enough = facts.disk_free_gb >= need_gb
        if not to_fetch:
            detail = "every model file is already in the models volume"
        elif enough:
            detail = (f"{facts.disk_free_gb:.0f} GB free at {facts.disk_path} for ~{need_gb:.1f} GB "
                      f"({len(to_fetch)} model files to fetch)")
        else:
            detail = (f"the {len(to_fetch)} model files still to fetch ({', '.join(f.file for f in to_fetch)}) "
                      f"need ~{need_gb:.1f} GB but only {facts.disk_free_gb:.0f} GB is free at {facts.disk_path}: "
                      f"free space or pick a smaller model (`model:` in ordo.yaml)")
        checks.append(Check("free disk for the models", enough, detail))

    busy = [f"{address}:{port}" for address, port in published_ports(services, env)
            if (address, port) in facts.busy_ports]
    checks.append(Check("host ports free", not busy,
                        "all free" if not busy else
                        f"already in use by another process: {', '.join(busy)} (stop it, then re-run)"))

    if secrets_path is not None:  # None: the caller checks secrets itself (`ordo preflight --secrets`)
        checks += secret_checks(secret_refs(services, secret_keys), optional_secrets, secrets_path)
    return checks


# ── Gathering the host facts (I/O; everything above is pure) ──────────────────
_DOCKER_PORT_RE = re.compile(r"(?:\d{1,3}(?:\.\d{1,3}){3}|\[[^\]]*\]):(\d+)(?:-(\d+))?->")


def parse_docker_ports(text: str) -> set[int]:
    """Host ports in `docker ps --format {{.Ports}}` text (`0.0.0.0:8443-8445->8443-8445/tcp`)."""
    held: set[int] = set()
    for m in _DOCKER_PORT_RE.finditer(text):
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first
        held.update(range(first, last + 1))
    return held


def _docker(*args: str) -> subprocess.CompletedProcess | None:  # pragma: no cover - shells to docker
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


_ADDRESS_IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


def _port_busy(address: str, port: int) -> bool:  # pragma: no cover - binds a socket
    """True when another process already holds (address, port). A bind refused for any other
    reason (a privileged port, an address this host lacks) is left for Docker to report."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((address, port))
        except OSError as e:
            return e.errno in _ADDRESS_IN_USE
    return False


def gather_host_facts(ports: list[tuple[str, int]], project: str,
                      fallback_disk_path: str) -> HostFacts:  # pragma: no cover - shells to docker
    info_proc = _docker("info", "--format", "{{json .}}")
    docker_error: str | None = None
    info: dict = {}
    if info_proc is None:
        docker_error = "the `docker` command was not found"
    elif info_proc.returncode != 0:
        lines = (info_proc.stderr or info_proc.stdout).strip().splitlines()
        docker_error = lines[-1] if lines else f"docker info exited {info_proc.returncode}"
    else:
        try:
            info = json.loads(info_proc.stdout)
        except ValueError:
            docker_error = "docker info returned unreadable output"

    version_proc = _docker("compose", "version", "--short")
    compose_version = None
    if version_proc is not None and version_proc.returncode == 0:
        compose_version = version_proc.stdout.strip() or None

    # Ports this project's own running containers publish are not a conflict: re-running `ordo up`
    # on a running stack keeps them.
    ours: set[int] = set()
    ps_proc = _docker("ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Ports}}")
    if ps_proc is not None and ps_proc.returncode == 0:
        ours = parse_docker_ports(ps_proc.stdout)
    busy = frozenset((address, port) for address, port in ports
                     if port not in ours and _port_busy(address, port))

    # The model lands in a Docker volume under the daemon's root. That path is only measurable when
    # the daemon runs on this host (Linux); Docker Desktop keeps it in its VM, so fall back to the
    # rendered stack's disk.
    root = str(info.get("DockerRootDir") or "")
    disk_path = root if root and os.path.isdir(root) else fallback_disk_path
    try:
        disk_free_gb: float | None = shutil.disk_usage(disk_path).free / 1024 ** 3
    except OSError:
        disk_free_gb = None

    volume_files = None
    if docker_error is None:
        listed = fetch.volume_files(fetch.DockerRunner(), project)
        volume_files = frozenset(listed) if listed is not None else None

    return HostFacts(docker_error=docker_error, compose_version=compose_version,
                     runtimes=frozenset((info.get("Runtimes") or {}).keys()), busy_ports=busy,
                     disk_path=disk_path, disk_free_gb=disk_free_gb, volume_files=volume_files)
