"""Read a secret the way the render delivers it: from the file `<NAME>_FILE` names, else from `<NAME>`.

A file-delivered secret (ordo/render/secret_files.py) reaches a container as a read-only file under
/run/secrets and an env var holding its path, so the value stays out of `docker inspect`. This is
the one reader every Python service of ours uses. The canonical copy is ordo/secret_env.py; each
image that needs it carries a byte-identical copy in its own build context (enforced by
tests/substrate/test_secret_files.py), so edit the canonical one and copy it over.
"""
from __future__ import annotations

import os
from collections.abc import Mapping


class SecretFileError(RuntimeError):
    """`<NAME>_FILE` is set but cannot be used. The message names variables and paths, never a value."""


def read_secret(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The value of secret `name`, stripped; "" when neither `<name>_FILE` nor `<name>` is set.

    A set `<name>_FILE` must point at a readable file: a declared file that is missing fails loud
    instead of silently reading as unset. Both set is refused, so a leftover env value can never
    shadow the file the render declared (or the other way round)."""
    env = os.environ if environ is None else environ
    path = env.get(f"{name}_FILE", "")
    if path:
        if env.get(name, ""):
            raise SecretFileError(f"both {name} and {name}_FILE are set; the render sets one of them")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            raise SecretFileError(f"{name}_FILE={path} cannot be read ({e.strerror})") from None
    return env.get(name, "").strip()
