"""The substrate digest: one hash of everything a render reads from the repo.

ops-controller renders from its own baked copy of ordo/, catalog/ and the services/ manifests,
while the operator renders from the checkout. When the two differ, a control-plane render silently
reverts whatever the newer side changed. Every render records this digest in out/manifest.json;
ops-controller compares its own against it before re-rendering, and `ordo doctor` compares the
running ops-controller's against the checkout's.

The inputs are exactly what render reads (tests/substrate/test_substrate_digest.py traces a render
and fails if it opens a repo file outside this set): the ordo/ code, the model catalog, and every
per-service render manifest. Build contexts (Dockerfiles, sources) are not render inputs.
"""
from __future__ import annotations

import functools
import hashlib
from pathlib import Path

# The repo root this package was loaded from (the checkout on the host, /app in the image).
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Per-service files render reads: the three manifest kinds and the dashboard card fragment.
SERVICE_MANIFEST_NAMES = ("plugin.yaml", "agent.yaml", "dashboard.yaml", "catalog.json")
# The LiteLLM proxy config render validates `litellm_key.models` grants against.
EXTRA_SERVICE_INPUTS = ("model-gateway/litellm_config.yaml",)


def _is_cache(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def substrate_files(root: str | Path) -> list[Path]:
    """Every render input under `root`, sorted by repo-relative path."""
    root = Path(root)
    files = [p for p in (root / "ordo").rglob("*.py") if not _is_cache(p)]
    files += [p for p in (root / "catalog").rglob("*") if p.is_file() and not _is_cache(p)]
    services = root / "services"
    for name in SERVICE_MANIFEST_NAMES:
        files += services.glob(f"*/{name}")
    files += [services / rel for rel in EXTRA_SERVICE_INPUTS if (services / rel).is_file()]
    return sorted(set(files), key=lambda p: p.relative_to(root).as_posix())


def substrate_digest(root: str | Path) -> str:
    """sha256 over each input's repo-relative path and contents (CRLF read as LF), hex encoded."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in substrate_files(root):
        content = path.read_bytes().replace(b"\r\n", b"\n")
        header = f"{path.relative_to(root).as_posix()}\0{len(content)}\0"
        digest.update(header.encode("utf-8") + content)
    return digest.hexdigest()


@functools.cache
def current_digest() -> str:
    """This process's own substrate digest, computed once: its files do not change under it."""
    return substrate_digest(PACKAGE_ROOT)


def substrate_verdict(ops_controller: str, reference: str, *, reference_name: str,
                      rebuild_from: str) -> tuple[bool, str]:
    """(ok, one-line report) comparing the running ops-controller's digest with `reference`.

    `ops_controller` is "" when its image predates the digest. The one judgment both callers make:
    `ordo doctor` compares the running ops-controller with the checkout (`reference_name` "this
    checkout"), and ops-controller's `GET /doctor` compares itself with the digest the last render
    recorded in out/manifest.json. A line starting "! " is a finding.
    """
    if ops_controller == reference:
        return True, f"substrate: ops-controller matches {reference_name} ({reference[:12]})"
    return False, (f"! substrate MISMATCH: ops-controller {ops_controller[:12] or '(predates the digest)'} vs "
                   f"{reference_name} {reference[:12]}. Its next model switch or plugin toggle would render from "
                   f"different inputs (it refuses with 409 once out/ records a digest). Rebuild "
                   f"ordo/ops-controller from {rebuild_from}, then `ordo recreate ops-controller`.")
