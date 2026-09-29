"""Bind-mounted config: label each service with the content digest of the config files it mounts.

Compose's config hash (the `com.docker.compose.config-hash` label `ordo apply` and ops-controller
compare, ordo/render/changed_set.py) covers a service's compose definition, not the contents of the
files it bind-mounts. An edited Prometheus rule or ClickHouse config therefore left apply reporting
"no change" while the service kept running the old file (#311, #304).

A service DECLARES which of its binds are config it reads at start (`config_mounts:` in its
manifest, the container paths), and each one renders as a label interpolated from
`out/bind-configs.env`, the way the file-secret digests work (ordo/render/secret_files.py):

    labels:
      ordo.bind-config./etc/prometheus/rules: ${ORDO_BIND_CONFIG_SHA256_PROMETHEUS_ETC_PROMETHEUS_RULES:-}

A declared bind must be a read-only bind of a checkout path (`${BASE_PATH...}/<path>`, not under
`data/`, `models/` or `out/`); anything else is a render error. A bind that is not declared (the
docs a RAG ingester watches, a certificate a service re-reads on every scrape, data) is never
digested, so no heuristic decides what counts as config.

The values are written by the HOST only (`write_values`, from `ordo render`, `ordo apply` and the
CLI's source edits), from the checkout at BASE_PATH. A control-plane render (a dashboard model
switch) never reads the checkout: it keeps out/bind-configs.env as the host last wrote it, so a
checkout edit never makes it recreate a service. Precisely:
  - `ordo apply` puts the previous values back when it recreates nothing (refused at the GPU lease,
    a failed preflight, unreadable state), so a later model switch does not deploy what it refused.
    Once it has brought the changed set up (even with a failure), the new values stay.
  - A plain `ordo render` or `ordo remote` writes new values without recreating anything, like the
    rest of out/ it writes; the next apply of out/, host or control plane, deploys them.
  - A bind is live: a service the control plane recreates for its own reason mounts the checkout as
    it is now, while its label keeps the host's digest. So when a declared file changed since the
    host's last apply, a model switch (which recreates llamacpp) starts llamacpp on the edited
    /llamacpp-scripts, and the next `ordo apply` recreates llamacpp once more to record the digest.
A file's digest is of its bytes; a directory's is of each file's relative path and bytes, skipping
Python caches. A source that does not exist is labelled ABSENT, so its arrival is a change too.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

# The label prefix, one label per declared config bind: `ordo.bind-config.<container path>`.
LABEL_PREFIX = "ordo.bind-config."
# The env file the host writes the digests to, next to .env; every compose call loads it.
VALUES_ENV_FILE = "bind-configs.env"
# The compose variable holding one bind's digest, in VALUES_ENV_FILE.
DIGEST_VAR_PREFIX = "ORDO_BIND_CONFIG_SHA256_"
# The digest of a bind whose source does not exist (docker would mount an empty directory).
ABSENT = "absent"
# Checkout-relative top directories that are never config: runtime state and the render's outputs
# (the render labels its own outputs from memory, `materialize` the secrets).
EXCLUDED_TOP_DIRS = ("data", "models", "out")
# Never part of a directory's digest: interpreter caches written by whoever ran the code.
SKIPPED_DIR_NAMES = frozenset({"__pycache__"})
SKIPPED_SUFFIXES = (".pyc",)

# The short bind syntax with a checkout source: `${BASE_PATH...}/<path>:<target>[:<options>]`. The
# source is matched first because its `${...}` may itself hold a colon.
_CHECKOUT_BIND = re.compile(
    r"^\$\{BASE_PATH(?:[:?-][^}]*)?\}/(?P<path>[^:]+):(?P<target>[^:]+)(?::(?P<options>.*))?$")


@dataclasses.dataclass(frozen=True)
class ConfigBind:
    service: str
    path: str        # relative to the checkout
    target: str      # the container path

    @property
    def var(self) -> str:
        return digest_var(self.service, self.target)


def digest_var(service: str, target: str) -> str:
    return DIGEST_VAR_PREFIX + re.sub(r"[^A-Z0-9]+", "_", f"{service}_{target}".upper()).strip("_")


def checkout_binds(volumes: Iterable[Any]) -> dict[str, str]:
    """container target -> checkout-relative source, for every read-only bind of a checkout path."""
    found: dict[str, str] = {}
    for volume in volumes:
        match = _CHECKOUT_BIND.match(volume) if isinstance(volume, str) else None
        if match and "ro" in (match.group("options") or "").split(","):
            found[match.group("target")] = match.group("path").strip("/")
    return found


def parse_config_mounts(where: str, raw: Any, volumes: Iterable[Any]) -> tuple[str, ...]:
    """Validate a manifest's `config_mounts:` (container paths) against the service's volumes."""
    targets = tuple(str(t) for t in (raw or []))
    binds = checkout_binds(volumes)
    repeated = sorted({t for t in targets if targets.count(t) > 1})
    if repeated:
        raise ValueError(f"{where}: config_mounts lists {repeated} more than once")
    for target in targets:
        if target not in binds:
            raise ValueError(f"{where}: config_mounts names {target!r}, which is not a read-only bind of a "
                             "checkout path (${BASE_PATH}/...:" + target + ":ro) in its volumes")
        parts = PurePosixPath(binds[target]).parts
        if not parts or parts[0] in EXCLUDED_TOP_DIRS or ".." in parts:
            raise ValueError(f"{where}: config_mounts names {target!r}, whose source {binds[target]!r} is "
                             f"runtime state or a render output ({', '.join(EXCLUDED_TOP_DIRS)}/), not config")
    return targets


def add_labels(service: str, spec: dict[str, Any], targets: Iterable[str]) -> None:
    """Label each declared config bind of `spec` with its digest variable (in place). `:-`: a value
    the host never wrote interpolates empty rather than failing every compose call."""
    targets = list(targets)
    if targets:
        parse_config_mounts(f"service {service!r}", targets, spec.get("volumes") or [])
    for target in targets:
        spec.setdefault("labels", {})[f"{LABEL_PREFIX}{target}"] = f"${{{digest_var(service, target)}:-}}"


def config_binds(doc: Mapping[str, Any]) -> list[ConfigBind]:
    """Every declared config bind in a rendered compose, read back from its labels."""
    found: list[ConfigBind] = []
    for name, spec in sorted((doc.get("services") or {}).items()):
        binds = checkout_binds((spec or {}).get("volumes") or [])
        for label in sorted((spec or {}).get("labels") or {}):
            if str(label).startswith(LABEL_PREFIX):
                target = str(label)[len(LABEL_PREFIX):]
                if target not in binds:
                    raise ValueError(f"{name}: label {label} names no read-only checkout bind")
                found.append(ConfigBind(name, binds[target], target))
    return found


def write_values(doc: Mapping[str, Any], env: Mapping[str, str], out_dir: str | Path) -> None:
    """Write out/bind-configs.env: each declared bind's digest, read from the checkout at BASE_PATH.
    The host's job only (see the module docstring). Without BASE_PATH the file is written empty:
    every `${BASE_PATH:?}` bind then fails compose, so nothing can run to drift."""
    base_path = str(env.get("BASE_PATH") or "")
    binds = config_binds(doc) if base_path else []
    lines = []
    seen: dict[str, ConfigBind] = {}
    for bind in binds:
        if bind.var in seen:
            raise ValueError(f"{bind.service} {bind.target} and {seen[bind.var].service} {seen[bind.var].target} "
                             f"share the digest variable {bind.var}")
        seen[bind.var] = bind
        lines.append(f"{bind.var}={digest_path(Path(base_path) / bind.path)}")
    Path(out_dir, VALUES_ENV_FILE).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def ensure_values_file(out_dir: str | Path) -> None:
    """Every compose call loads the values file and compose refuses a missing --env-file, so a render
    makes sure it exists; only `write_values` changes its content."""
    path = Path(out_dir, VALUES_ENV_FILE)
    if not path.exists():
        path.write_text("", encoding="utf-8")


def digest_path(path: Path) -> str:
    """The sha256 of a file's bytes, or of a directory's files (each relative path and its bytes, in
    path order, Python caches skipped); ABSENT when the path does not exist."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        return ABSENT
    files: dict[str, Path] = {}
    for directory, subdirs, names in os.walk(path):
        subdirs[:] = [d for d in subdirs if d not in SKIPPED_DIR_NAMES]
        for name in names:
            file = Path(directory) / name
            if file.is_file() and not name.endswith(SKIPPED_SUFFIXES):
                files[file.relative_to(path).as_posix()] = file
    digest = hashlib.sha256()
    for rel in sorted(files):
        content = files[rel].read_bytes()
        digest.update(f"{rel}\0{len(content)}\0".encode() + content)
    return digest.hexdigest()
