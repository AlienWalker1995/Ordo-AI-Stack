"""The rendered stack as `docker compose` sees it: reading out/docker-compose.yml, and the ONE
builder of a `docker compose` invocation against it.

ops-controller's `DockerBackend._compose` and the host CLI (`ordo up`, `ordo recreate`, `ordo apply`)
both call `compose_argv`, so the env files and the profile set cannot diverge between the control
plane and the operator's shell. The queries over the rendered compose (`lifecycle_group`,
`plan_named`, `readers_of`, ...) are pure functions of the file render wrote, shared the same way.
"""
from __future__ import annotations

import re
from collections.abc import Sequence

import yaml

from . import secret_files

COMPOSE_FILE = "docker-compose.yml"

# ${VAR}, ${VAR:-default} or ${VAR:?message}: the compose interpolation a rendered value may carry
# (e.g. `${COMFYUI_IMAGE:-yanwk/comfyui-boot@sha256:...}`, `${CADDY_BIND:?...}:443:443`). Resolved
# against the rendered .env (with the `:-default` fallback) so a check compares the ACTUAL value.
COMPOSE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")


def expand_env(value: str, env: dict[str, str]) -> str:
    """`value` with every compose `${VAR...}` reference resolved against the rendered `env`."""
    def sub(m: re.Match[str]) -> str:
        val = env.get(m.group(1))
        if val not in (None, ""):
            return val
        is_default = (m.group(2) or "").endswith("-")
        return (m.group(3) or "") if is_default else ""
    return COMPOSE_VAR_RE.sub(sub, value)


def compose_argv(compose_dir: str, project: str, *args: str, profiles: Sequence[str] = ()) -> list[str]:
    """`docker compose` against the rendered stack in `compose_dir`, followed by `args`.

    `profiles` widens the resolvable set so a target whose `depends_on:` names a profiled service
    resolves (without it `docker compose ... open-webui` aborts with "no such service: qdrant").
    Widening is safe; `--no-deps` is what limits a start to the named services.

    Every env file, always. Passing any --env-file disables compose's implicit .env auto-load, so
    .env must be listed too; without secrets.env every ${LITELLM_MASTER_KEY} style reference goes
    UNSET and secret-dependent services crash-loop (the 2026-06-26 oauth2-proxy 11-byte-cookie
    outage). secret-files.env holds the digest each file-secret mount is labelled with, so a
    rotated file secret changes its readers' config hash (ordo/render/secret_files.py). Order matters:
    derived first, secrets second.
    """
    cmd = ["docker", "compose", "-p", project, "-f", f"{compose_dir}/{COMPOSE_FILE}"]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += [
        "--env-file", f"{compose_dir}/.env",
        "--env-file", f"{compose_dir}/secrets.env",
        "--env-file", f"{compose_dir}/{secret_files.DIGESTS_ENV_FILE}",
    ]
    return cmd + list(args)


def load_compose(compose_dir: str) -> dict:
    """The rendered compose file as a dict. Raises OSError / yaml.YAMLError when unreadable."""
    with open(f"{compose_dir}/{COMPOSE_FILE}", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def profiles_in(doc: dict) -> list[str]:
    """Every profile named anywhere in the rendered compose, sorted for determinism."""
    found: set[str] = set()
    for service in (doc.get("services") or {}).values():
        for profile in (service or {}).get("profiles") or []:
            found.add(str(profile))
    return sorted(found)


def services_of(doc: dict) -> dict:
    """The rendered compose's `services:` map ({} when it has none)."""
    return doc.get("services") or {}


def netns_members(doc: dict, owner: str) -> list[str]:
    """Services declared with `network_mode: service:<owner>`, sorted."""
    return sorted(
        name for name, spec in services_of(doc).items()
        if (spec or {}).get("network_mode") == f"service:{owner}"
    )


def lifecycle_group(doc: dict, service: str) -> list[str]:
    """`service` followed by the services living in its network namespace.

    The ONE place that knows which services follow which. A member shares its owner's network
    namespace, and anything that gives the owner a new one (restart, stop and start, a `--no-deps`
    recreate, a named down) leaves the member attached to the dead namespace: still running,
    still "healthy", with only `lo`. So every verb that cycles the owner cycles the group, owner
    first. A member (or any service nothing joins) is a group of one: acting on a member acts on
    the member only. The host CLI (`plan_named`) and ops-controller's lifecycle verbs both expand
    through here, so they cannot disagree about who follows whom.
    """
    return [service] + [m for m in netns_members(doc, service) if m != service]


def _strings(node: object) -> list[str]:
    """Every string in a compose service definition (keys and values, at any depth)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in _strings(key) + _strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def readers_of(doc: dict, keys: Sequence[str]) -> list[str]:
    """The long-running services whose rendered definition interpolates any of `keys`, sorted.

    Read from the rendered compose, so it covers every way a service reads a key: a declared
    `secrets:` entry (`KEY: ${KEY}`), a key mapped onto another name (`X: ${KEY:-}`), a command
    line (`--requirepass ${KEY}`), or a file secret (its mount's digest label,
    `${ORDO_SECRET_FILE_SHA256_KEY:-}`). A one-shot job (`restart: "no"`, the evals runner) is left
    out: it reads its environment on each run, and recreating it would start one.
    """
    names = [*keys, *(secret_files.digest_var(key) for key in keys)]
    refs = [re.compile(r"\$\{" + re.escape(name) + r"[}:?-]") for name in names]
    readers = []
    for name, spec in services_of(doc).items():
        if str((spec or {}).get("restart")) == "no":
            continue
        if any(ref.search(text) for text in _strings(spec) for ref in refs):
            readers.append(name)
    return sorted(readers)


def plan_named(doc: dict, services: Sequence[str], *, force_recreate: bool) -> tuple[list[str], set[str]]:
    """(compose args, services compose will start) for a named-service bring-up.

    Named services are started alone (`--no-deps`), each with its `lifecycle_group`: the netns
    members are listed by name so compose recreates them after their owner (it still orders named
    services by `depends_on` under `--no-deps`), while the owner's own dependencies are left alone.
    """
    targets: list[str] = []
    for service in services:
        targets += [name for name in lifecycle_group(doc, service) if name not in targets]
    args = ["up", "-d", "--no-deps"] + (["--force-recreate"] if force_recreate else []) + targets
    return args, set(targets)


def starting_services(doc: dict, services: Sequence[str], *, whole_stack: bool, with_profiles: bool) -> dict:
    """The compose definitions of the services this bring-up starts (what preflight checks)."""
    defined = services_of(doc)
    if whole_stack:
        return {name: spec for name, spec in defined.items()
                if with_profiles or not (spec or {}).get("profiles")}
    _args, targets = plan_named(doc, services, force_recreate=False)
    return {name: defined[name] for name in sorted(targets) if name in defined}
