"""File-delivered secrets: a secret a service reads from a read-only file instead of its environment.

A value in a container's environment is readable by anyone who can `docker inspect` it (every
holder of the Docker socket, the agent included) and it leaks into every diagnostic dump of the
container config. A file-delivered secret is not: the service's manifest lists it under
`secret_files:`, `ordo secrets materialize` writes its value to `out/secrets/<file>`, and the
renderer bind-mounts that file read-only at `/run/secrets/<file>` and sets ONE environment variable
to the path (`<KEY>_FILE` by default, the convention postgres, oauth2-proxy and our own services
read). The container config then holds the path, never the value.

One manifest shape everywhere (plugin services, MCP servers, the agent, the dashboard):

    secret_files:
      - OPS_CONTROLLER_TOKEN                               # -> OPS_CONTROLLER_TOKEN_FILE=/run/secrets/ops_controller_token
      - {key: LITELLM_DB_PASSWORD, env: POSTGRES_PASSWORD_FILE}   # the name the image reads
      - {key: TS_AUTHKEY, env: TS_AUTHKEY, prefix: "file:"}       # -> TS_AUTHKEY=file:/run/secrets/ts_authkey

`prefix` is for software that takes the path inside a value of its own shape (tailscale's
containerboot reads `TS_AUTHKEY=file:<path>`); it is literal text, never a secret.

The file name is the key lowercased, so the key/file mapping is a bijection and the rendered compose
alone says which keys a service reads from files (`secret_files_in`).

A file's content is not part of a container's config, so a rotated value would not change the
service's compose config hash and `ordo apply` would leave the old value running. Each mount
therefore carries a label interpolated from `out/secret-files.env`, which `materialize` writes with
a digest of every file secret: a new value changes the label, the hash, and so the recreate set.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

# Where `ordo secrets materialize` writes the file secrets: out/secrets/ under the checkout. A host path
# (${BASE_PATH}), because ops-controller recreates services with compose running in /config.
SECRET_FILES_DIR = "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out/secrets"
# Where each file lands inside the container (the Docker secrets convention).
SECRET_FILES_TARGET_DIR = "/run/secrets"
# The env file `materialize` writes the digests to, next to secrets.env; every compose call loads it.
DIGESTS_ENV_FILE = "secret-files.env"
# The label prefix on a service, one label per mounted file: `ordo.secret-file.<file>`.
LABEL_PREFIX = "ordo.secret-file."
# The compose variable holding one key's digest, in DIGESTS_ENV_FILE.
DIGEST_VAR_PREFIX = "ORDO_SECRET_FILE_SHA256_"

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PREFIX = re.compile(r"^[A-Za-z0-9_.:/-]*$")


@dataclasses.dataclass(frozen=True)
class SecretFileRef:
    """One file-delivered secret of one service: the store `key`, and the `env` var that gets its
    path (after the literal `prefix`, usually empty)."""
    key: str
    env: str
    prefix: str = ""

    @property
    def file(self) -> str:
        return self.key.lower()

    @property
    def target(self) -> str:
        return f"{SECRET_FILES_TARGET_DIR}/{self.file}"

    @property
    def source(self) -> str:
        return f"{SECRET_FILES_DIR}/{self.file}"

    @property
    def env_value(self) -> str:
        return f"{self.prefix}{self.target}"


def parse_secret_files(where: str, raw: Any, *, env_secrets: Iterable[str] = (),
                       explicit_env: Iterable[str] = ()) -> tuple[SecretFileRef, ...]:
    """Parse a manifest's `secret_files:` list.

    Each entry is a key NAME (delivered as `<KEY>_FILE`) or `{key: NAME, env: NAME, prefix: TEXT}`
    (`env` and `prefix` optional). Refused: a malformed entry, a key listed twice, a key also
    delivered by env (`secrets:`), and an `env` the manifest also sets in its own env block (two
    sources for one variable)."""
    refs: list[SecretFileRef] = []
    for entry in raw or []:
        prefix = ""
        if isinstance(entry, str):
            key, env = entry, f"{entry}_FILE"
        elif isinstance(entry, Mapping) and "key" in entry and set(entry) <= {"key", "env", "prefix"}:
            key = str(entry.get("key", ""))
            env = str(entry.get("env") or f"{key}_FILE")
            prefix = str(entry.get("prefix") or "")
        else:
            raise ValueError(f"{where}: secret_files entry {entry!r} must be a key NAME or "
                             "{key: NAME, env: NAME, prefix: TEXT}")
        if not _ENV_NAME.match(key) or not _ENV_NAME.match(env):
            raise ValueError(f"{where}: secret_files entry {entry!r}: key and env must be UPPER_SNAKE names")
        if not _PREFIX.match(prefix):
            raise ValueError(f"{where}: secret_files entry {entry!r}: prefix must be plain text like `file:`")
        refs.append(SecretFileRef(key=key, env=env, prefix=prefix))
    keys = [r.key for r in refs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise ValueError(f"{where}: {repeated} listed more than once in secret_files")
    envs = [r.env for r in refs]
    repeated_env = sorted({e for e in envs if envs.count(e) > 1})
    if repeated_env:
        raise ValueError(f"{where}: secret_files env {repeated_env} used more than once")
    both = sorted(set(keys) & set(env_secrets))
    if both:
        raise ValueError(f"{where}: {both} is in `secrets:` (env) AND `secret_files:`; deliver it one way")
    clash = sorted(set(envs) & set(explicit_env))
    if clash:
        raise ValueError(f"{where}: {clash} is set in the env block AND by secret_files; keep one")
    return tuple(refs)


def digest_var(key: str) -> str:
    return f"{DIGEST_VAR_PREFIX}{key}"


def digest(value: str) -> str:
    """A short digest of a secret value: enough to see that it changed, not to recover it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def add_to_service(service: dict[str, Any], refs: Iterable[SecretFileRef]) -> None:
    """Mount each file read-only, point its env var at it, and label the mount with the value's
    digest (in place). An env var the service already sets is refused: the manifest parsers catch
    that first, and this keeps the renderer from silently overwriting one."""
    refs = list(refs)
    if not refs:
        return
    env = service.setdefault("environment", {})
    volumes = service.setdefault("volumes", [])
    labels = service.setdefault("labels", {})
    for ref in refs:
        if ref.env in env:
            raise ValueError(f"{ref.env} is already set on this service; it cannot also carry the {ref.key} file path")
        env[ref.env] = ref.env_value
        volumes.append(f"{ref.source}:{ref.target}:ro")
        # `:-`: a digest that was never materialized interpolates empty rather than failing every
        # compose call (a dry run before the first materialize, a fresh checkout).
        labels[f"{LABEL_PREFIX}{ref.file}"] = f"${{{digest_var(ref.key)}:-}}"


def secret_files_in(doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(service, key) for every file secret the rendered compose mounts, from the labels
    add_to_service writes. The key is the file name uppercased (the file name is the key lowercased)."""
    found: list[tuple[str, str]] = []
    for name, spec in (doc.get("services") or {}).items():
        for label in ((spec or {}).get("labels") or {}):
            if str(label).startswith(LABEL_PREFIX):
                found.append((name, str(label)[len(LABEL_PREFIX):].upper()))
    return found
