"""Bind-mounted config: label each service with the content digest of the config files it mounts.

Compose's config hash (the `com.docker.compose.config-hash` label `ordo apply` and ops-controller
compare, ordo/render/changed_set.py) covers a service's compose definition, not the contents of the
files it bind-mounts. An edited Prometheus rule or ClickHouse config therefore left apply reporting
"no change" while the service kept running the old file (#311, #304). The render now labels each
config bind with a digest of what it mounts, `ordo.bind-config.<container path>: <sha256>`, so a
content change moves the label, the config hash, and so the changed set: apply recreates exactly
the services whose mounted config changed. The file-secret labels (ordo/render/secret_files.py) do
the same for the materialized secrets, and `compose.RENDERED_CONFIG_LABEL` for the files the render
writes itself.

The rule for which binds are config, applied to every service alike:
  - read-only (`:ro`, or `read_only: true`): a writable bind is state the service writes;
  - the source is a path under the checkout, written `${BASE_PATH...}/<path>`: DATA_PATH,
    MEMORY_VAULT_PATH, CODE_ROOT, the Docker socket and named volumes are not the checkout;
  - not runtime state or a render output: nothing under `data/`, `models/` (the runtime dirs that
    are never committed), `out/` (the render labels its own outputs from memory, and `materialize`
    the secrets), or the site's DATA_PATH wherever it lives;
  - a file, or a directory of at most MAX_DIR_FILES files and MAX_DIR_BYTES bytes: a larger tree is
    watched data (the docs a RAG ingester reads), not config read at startup.
A source that does not exist is labelled ABSENT, so its arrival is a change too.

The host reads the checkout at BASE_PATH. ops-controller re-renders inside a container where
BASE_PATH names a host path it cannot open, so it mounts the checkout read-only at
OPS_CONTROLLER_CHECKOUT_DIR and names it in CHECKOUT_DIR_ENV: both sides then run this one function
over the same files and render the same labels.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

# The label prefix, one label per config bind: `ordo.bind-config.<container path>`.
LABEL_PREFIX = "ordo.bind-config."
# The label value of a bind whose source does not exist (docker would create an empty directory).
ABSENT = "absent"
# Where this process reads the checkout, when BASE_PATH (a host path) is not readable here.
CHECKOUT_DIR_ENV = "ORDO_CHECKOUT_DIR"
# ops-controller's read-only mount of the checkout.
OPS_CONTROLLER_CHECKOUT_DIR = "/checkout"
# The largest directory digested as config. Beyond either bound it is data, and left out.
MAX_DIR_FILES = 64
MAX_DIR_BYTES = 4 * 1024 * 1024
# Checkout-relative top directories that are never config: runtime state and the render's outputs.
EXCLUDED_TOP_DIRS = ("data", "models", "out")

# `${BASE_PATH}`, `${BASE_PATH:?message}` or `${BASE_PATH:-default}`, then `/<path>`.
_CHECKOUT_SOURCE = re.compile(r"^\$\{BASE_PATH(?:[:?-][^}]*)?\}/(?P<path>.+)$")
# The short syntax with a checkout source: `${BASE_PATH...}/<path>:<target>[:<options>]`. The
# source is matched first because its `${...}` may itself hold a colon.
_SHORT_BIND = re.compile(r"^(?P<source>\$\{BASE_PATH(?:[:?-][^}]*)?\}/[^:]+):(?P<target>[^:]+)(?::(?P<options>.*))?$")


def add_labels(services: dict[str, Any], env: Mapping[str, str]) -> None:
    """Label every service's config binds with their content digest (in place).

    `env` is the render's .env (BASE_PATH, DATA_PATH). Without BASE_PATH nothing is labelled: every
    `${BASE_PATH:?}` bind then fails compose, so nothing can run to drift. The host reads the checkout
    at BASE_PATH; where BASE_PATH does not exist either, each bind is labelled ABSENT, which is what
    its container would mount. ops-controller reads it at its mount (CHECKOUT_DIR_ENV), and a mount
    that cannot be read raises ValueError: labelling every bind absent there would recreate them all."""
    base_path = str(env.get("BASE_PATH") or "")
    if not base_path:
        return
    mounted = os.environ.get(CHECKOUT_DIR_ENV)
    if mounted and not Path(mounted).is_dir():
        raise ValueError(f"cannot digest the bind-mounted config: {CHECKOUT_DIR_ENV}={mounted} is not a readable "
                         f"directory (ops-controller mounts the checkout at {OPS_CONTROLLER_CHECKOUT_DIR})")
    root = Path(mounted or base_path)
    data_rel = _relative_to(str(env.get("DATA_PATH") or ""), base_path)
    for spec in services.values():
        if not spec:
            continue
        for volume in spec.get("volumes") or []:
            bind = _config_bind(volume)
            if bind is None:
                continue
            rel, target = bind
            if _is_excluded(rel, data_rel):
                continue
            digest = digest_path(root / rel)
            if digest is not None:
                spec.setdefault("labels", {})[f"{LABEL_PREFIX}{target}"] = digest


def digest_path(path: Path) -> str | None:
    """The sha256 of a file's bytes, or of a directory's files (each relative path and its bytes, in
    path order); ABSENT when the path does not exist; None for a directory too large to be config."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        return ABSENT
    # Walked with an early exit, so a large tree costs no more than the limit to rule out.
    files: dict[str, Path] = {}
    total = 0
    for directory, _subdirs, names in os.walk(path):
        for name in names:
            file = Path(directory) / name
            if not file.is_file():
                continue
            files[file.relative_to(path).as_posix()] = file
            total += file.stat().st_size
            if len(files) > MAX_DIR_FILES or total > MAX_DIR_BYTES:
                return None
    digest = hashlib.sha256()
    for rel in sorted(files):
        content = files[rel].read_bytes()
        digest.update(f"{rel}\0{len(content)}\0".encode() + content)
    return digest.hexdigest()


def _config_bind(volume: Any) -> tuple[str, str] | None:
    """(checkout-relative source, container target) for a read-only bind of a checkout path."""
    if isinstance(volume, str):
        match = _SHORT_BIND.match(volume)
        if match is None or "ro" not in (match.group("options") or "").split(","):
            return None
        source, target = match.group("source"), match.group("target")
    elif isinstance(volume, Mapping):
        if volume.get("type") != "bind" or volume.get("read_only") is not True:
            return None
        source, target = str(volume.get("source") or ""), str(volume.get("target") or "")
    else:
        return None
    match = _CHECKOUT_SOURCE.match(source)
    if match is None or not target:
        return None
    return match.group("path").strip("/"), target


def _is_excluded(rel: str, data_rel: str | None) -> bool:
    parts = PurePosixPath(rel).parts
    if not parts or parts[0] in EXCLUDED_TOP_DIRS or ".." in parts:
        return True
    return data_rel is not None and (data_rel == "" or rel == data_rel or rel.startswith(f"{data_rel}/"))


def _relative_to(path: str, base: str) -> str | None:
    """`path` relative to `base` ("" when equal), None when it is not under it. Both are host paths
    as the site writes them (C:/dev/ordo or /srv/ordo), compared with forward slashes."""
    def normal(value: str) -> str:
        return value.replace("\\", "/").rstrip("/")

    path, base = normal(path), normal(base)
    if not path or not base:
        return None
    if path == base:
        return ""
    return path[len(base) + 1:] if path.startswith(f"{base}/") else None
