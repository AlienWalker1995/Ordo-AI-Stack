"""The models volume: where the model files live, which of them the rendered stack reads, and
listing what the volume holds.

llama.cpp and the other model services read their weights from the `models-gguf` named volume, not a
host directory. The render decides which files each service loads (the chat model and projector
from .env, every other service from its command line); this module reads that back from the
rendered compose + .env. Both sides use it: ops-controller lists the volume for the model switch,
and the host fetch (ordo/host/fetch.py) downloads what is missing into it.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping, Sequence

from .stack import expand_env

# The helper image that lists (here) and downloads into (ordo/host/fetch.py) the volume: curl +
# busybox (sha256sum, mv) and nothing else. Pinned by version AND digest.
HELPER_IMAGE = ("curlimages/curl:8.22.0"
                "@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777")
MODEL_VOLUME = "models-gguf"            # the compose volume the llama.cpp services mount
VOLUME_MOUNT = "/models"                # where the helper mounts it
CHAT_SERVICE = "llamacpp"               # its launcher reads LLAMACPP_MODEL / LLAMACPP_MMPROJ from .env
# The label on a listing helper container (the download helper carries fetch.FETCH_MARKER).
LIST_MARKER = "ordo.model-fetch=list"


@dataclasses.dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str


class DockerRunner:  # pragma: no cover - shells to docker
    """The docker CLI. `env` None inherits this process's environment; `capture` returns stdout
    instead of streaming it (the download progress streams to the terminal)."""

    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
            capture: bool = False) -> RunResult:
        try:
            proc = subprocess.run(list(argv), env=dict(env) if env is not None else None,
                                  capture_output=capture, text=True)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"cannot run docker: {e}", file=sys.stderr)
            return RunResult(127, "")
        return RunResult(proc.returncode, proc.stdout or "")


def volume_name(project: str) -> str:
    """The Docker name compose gives the project's models-gguf volume."""
    return f"{project}_{MODEL_VOLUME}"


def _list_argv(volume: str) -> list[str]:
    return ["docker", "run", "--rm", "--label", LIST_MARKER, "-v", f"{volume}:{VOLUME_MOUNT}:ro",
            "--entrypoint", "ls", HELPER_IMAGE, "-1A", VOLUME_MOUNT]


def volume_exists(runner, volume: str) -> bool:
    return runner.run(["docker", "volume", "inspect", volume], capture=True).returncode == 0


def files_in_volume(runner, volume: str) -> set[str] | None:
    """The file names in the volume, or None when it could not be listed."""
    result = runner.run(_list_argv(volume), capture=True)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def volume_files(runner, project: str) -> set[str] | None:
    """The file names in the project's models volume: empty when the volume does not exist yet,
    None when it could not be listed. Checks first, because listing a missing volume with `docker
    run -v` would create it without the labels compose needs to adopt it."""
    volume = volume_name(project)
    if not volume_exists(runner, volume):
        return set()
    return files_in_volume(runner, volume)


@dataclasses.dataclass(frozen=True)
class NeededFile:
    file: str
    service: str
    optional: bool = False       # the service runs without it (a vision projector)


def _model_mount(spec: dict) -> str | None:
    """Where the service mounts the models volume, or None when it does not."""
    for volume in spec.get("volumes") or []:
        if isinstance(volume, str) and volume.split(":")[0] == MODEL_VOLUME:
            return volume.split(":")[1]
    return None


def required_model_files(doc: dict, env: Mapping[str, str], services: Sequence[str]) -> list[NeededFile]:
    """Every file in the models volume that `services` load, read from the rendered compose + .env.

    The chat service's launcher takes its file from LLAMACPP_MODEL (and the optional projector from
    LLAMACPP_MMPROJ); every other service names `<mount>/<file>` in its command."""
    found: dict[str, NeededFile] = {}
    defined = doc.get("services") or {}
    for name in services:
        spec = defined.get(name) or {}
        mount = _model_mount(spec)
        if mount is None:
            continue
        prefix = mount.rstrip("/") + "/"
        candidates: list[tuple[str, bool]] = []
        if name == CHAT_SERVICE:
            candidates.append((prefix + env.get("LLAMACPP_MODEL", ""), False))
            candidates.append((env.get("LLAMACPP_MMPROJ", ""), True))
        command = spec.get("command") or []
        for arg in command.split() if isinstance(command, str) else command:
            candidates.append((expand_env(str(arg), dict(env)), False))
        for path, optional in candidates:
            file = path[len(prefix):] if path.startswith(prefix) else ""
            if not file:
                continue
            known = found.get(file)
            if known is None or (known.optional and not optional):
                found[file] = NeededFile(file, name, optional)
    return list(found.values())
