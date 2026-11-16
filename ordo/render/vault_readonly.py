"""Read-only folders inside the memory vault: the operator's canonical docs, which agents must not write.

The vault is mounted read-write into the memory-vault MCP server (`/vault`) and, through its read-write
binds, into the agent. `site: MEMORY_VAULT_READONLY_DIRS` names folders of the vault (comma-separated,
relative to the vault root, for example `notes/Canonical`). The render mounts each one a second time,
read-only, on top of the read-write bind. Unset or empty adds nothing.

What that guarantees: the mount refuses a write made by the folder's own path. On a case-sensitive host
filesystem that is every path. On a case-insensitive one (a Windows bind) a differently cased path
(`notes/canonical`) reaches the same files through the read-write bind and skips the overlay, so there it
stops accidental and rule-following writes, not a determined one; the render warns about that. Full
enforcement needs the vault outside every read-write bind of the agent, or a case-sensitive filesystem.
The agent's Docker socket also lets it start a container with its own read-write bind.

Two services mount the vault, and each gets its own targets:
  - the MCP server mounts the folder at `/vault/<dir>`.
  - the agent does not mount the vault directly. It sees it through every read-write bind of the agent
    whose host source contains the vault (the code-root mirror, `${DATA_PATH}:/workspace/data`, ...), at
    `<bind target>/<vault path below the bind source>/<dir>`: one nested read-only bind per such bind.
    When no agent bind contains the vault the agent cannot see it: nothing is mounted, with a warning.

The render cannot stat host paths (ops-controller renders it too), so a folder that does not exist is
refused by the host preflight (`ordo/host/preflight.py`), before Docker can create an empty one.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .stack import expand_env

READONLY_DIRS_KEY = "MEMORY_VAULT_READONLY_DIRS"
VAULT_PATH_KEY = "MEMORY_VAULT_PATH"
MCP_PLUGIN_ID = "memory-vault"
MCP_VAULT_TARGET = "/vault"                    # the image CMD's `mcpvault /vault`
VAULT_SOURCE = "${MEMORY_VAULT_PATH:?MEMORY_VAULT_PATH must be set in ordo.yaml site}"

# Characters that would break a compose short-syntax mount or be interpolated by compose.
_UNSAFE_CHARACTERS = re.compile(r"[\\:$\x00-\x1f]")
_DRIVE_PREFIX = re.compile(r"^([A-Za-z]):(?=/|$)")
# A short-syntax bind `<source>:<target>[:<options>]`. The source may hold a drive colon (`C:/dev`), so it
# is greedy and the target must start with `/`.
_GIT_BASH_DRIVE = re.compile(r"^/[A-Za-z](?:/|$)")  # /c/dev, how Git Bash and Docker spell C:/dev
_BIND = re.compile(r"^(?P<source>.+):(?P<target>/[^:]*)(?::(?P<options>[a-z,]+))?$")


def parse_readonly_dirs(raw: object) -> list[str]:
    """The folders `MEMORY_VAULT_READONLY_DIRS` names, validated; [] when unset or blank."""
    text = str(raw or "").strip()
    if not text:
        return []
    folders: list[str] = []
    for entry in text.split(","):
        folder = entry.strip().rstrip("/")
        problem = _unsafe_reason(folder)
        if problem:
            raise ValueError(f"site: {READONLY_DIRS_KEY} entry {entry.strip()!r} {problem}; list folders "
                             "relative to the vault root, for example notes/Canonical")
        if folder not in folders:
            folders.append(folder)
    return folders


def _unsafe_reason(folder: str) -> str:
    if not folder:
        return "is empty"
    if _UNSAFE_CHARACTERS.search(folder):
        return "has a backslash, colon, dollar sign or control character"
    if folder.startswith("/"):
        return "is an absolute path"
    segments = folder.split("/")
    if "" in segments:
        return "has an empty path segment"
    if ".." in segments or "." in segments:
        return "has a '.' or '..' segment"
    return ""


def mcp_mounts(folders: list[str]) -> list[str]:
    """The nested read-only binds of the memory-vault MCP server, one per folder."""
    return [f"{VAULT_SOURCE}/{folder}:{MCP_VAULT_TARGET}/{folder}:ro" for folder in folders]


def agent_mounts(folders: list[str], vault_path: str, agent_volumes: list[str],
                 env: dict[str, str]) -> tuple[list[str], list[str]]:
    """(nested read-only binds, warnings) for the agent: see the module docstring. `env` resolves the
    `${VAR}` references of the agent's bind sources."""
    vault = _normalized(vault_path)
    mounts: list[str] = []
    for volume in agent_volumes:
        bind = _BIND.match(expand_env(volume, env))
        if bind is None or "ro" in (bind["options"] or "").split(","):
            continue
        source = _normalized(bind["source"])
        below_source = _relative_to(vault, source) if source.is_absolute() else None
        if below_source is None:
            continue
        vault_in_agent = PurePosixPath(bind["target"]) / below_source
        mounts += [f"{VAULT_SOURCE}/{folder}:{vault_in_agent / folder}:ro" for folder in folders]
    if not mounts:
        return [], [f"{READONLY_DIRS_KEY}: no read-write bind of the agent contains {VAULT_PATH_KEY}, so the "
                    "agent cannot reach the vault and no read-only folder is mounted in it"]
    return mounts, []


def _normalized(host_path: str) -> PurePosixPath:
    """A host path as one POSIX spelling, so `C:\\dev` and `/c/dev` compare equal."""
    text = host_path.strip().replace("\\", "/")
    text = _DRIVE_PREFIX.sub(lambda match: "/" + match.group(1).lower(), text)
    return PurePosixPath(text)


def _relative_to(path: PurePosixPath, root: PurePosixPath) -> PurePosixPath | None:
    """`path` below `root`, compared without regard to case (a Windows host spells one folder several
    ways); the result keeps the spelling of `path`."""
    root_parts = [part.casefold() for part in root.parts]
    if [part.casefold() for part in path.parts[:len(root_parts)]] != root_parts:
        return None
    return PurePosixPath(*path.parts[len(root_parts):])


def case_bypass_warning(vault_path: str) -> list[str]:
    """One warning when the vault looks like a Windows path, where the overlay can be bypassed."""
    spelling = vault_path.strip().replace("\\", "/")
    if not (_DRIVE_PREFIX.match(spelling) or _GIT_BASH_DRIVE.match(spelling)):
        return []
    return [f"{READONLY_DIRS_KEY}: {VAULT_PATH_KEY} looks like a Windows path, whose file system ignores case: "
            "a write by a differently cased path (notes/canonical for notes/Canonical) skips the read-only "
            "mount, so here it stops accidental writes, not a determined one"]
