"""Model provisioning — download catalog models with MANDATORY checksum verification.

The corrupt-weights lesson is a hard rule here: a model is only trusted if its bytes hash to the
pinned sha256. So:
  - a model whose catalog entry has no sha256 is REFUSED for download unless the operator explicitly
    passes --allow-unverified (never silently trust unpinned weights),
  - after a download the file is hashed and DELETED if it doesn't match (never leave corrupt weights
    on disk to half-load into noise),
  - an already-present, already-verified file is skipped — so once fetched, installs are offline.

The hashing + planning + verify-and-reject logic is pure and fully tested; only the actual network
download shells out (injected, so tests exercise the whole fetch/verify/reject path with a fake).
"""
from __future__ import annotations

import dataclasses
import hashlib
import urllib.request
from pathlib import Path
from typing import Callable

from .catalog import Catalog, Model

# action codes a plan can produce
OK = "ok"                       # present + verified — nothing to do (offline-ready)
DOWNLOAD = "download"           # missing, will verify after fetch
REDOWNLOAD = "redownload"       # present but sha256 mismatch — corrupt/wrong, refetch
REFUSE = "refuse-no-checksum"   # would need to fetch but sha256 is null and not --allow-unverified
UNVERIFIED = "present-unverified"  # present, sha256 null — cannot verify (informational)


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def classify(model: Model, models_dir: str | Path) -> str:
    """Status of a model's file on disk relative to its pinned checksum."""
    p = Path(models_dir) / model.file
    if not p.exists():
        return "missing"
    if not model.sha256:
        return "present-unverified"     # present but nothing to verify against
    return "verified" if sha256_file(p) == model.sha256 else "mismatch"


@dataclasses.dataclass
class Action:
    model_id: str
    action: str
    reason: str


def plan(catalog: Catalog, wanted: list[str] | None, models_dir: str | Path,
         allow_unverified: bool = False) -> list[Action]:
    """What `fetch` would do for each requested model (or all). Pure — reads the filesystem."""
    models = catalog.models if not wanted else [m for m in catalog.models if m.id in set(wanted)]
    out: list[Action] = []
    for m in models:
        status = classify(m, models_dir)
        if status == "verified":
            out.append(Action(m.id, OK, "present and checksum-verified"))
        elif status == "present-unverified":
            out.append(Action(m.id, UNVERIFIED, "present but no sha256 to verify against"))
        elif status == "mismatch":
            out.append(Action(m.id, REDOWNLOAD, "on-disk file does not match pinned sha256"))
        else:  # missing
            if not m.sha256 and not allow_unverified:
                out.append(Action(m.id, REFUSE,
                                  "no sha256 pinned — refuse (pass --allow-unverified to override)"))
            else:
                out.append(Action(m.id, DOWNLOAD, "will download and verify"))
    return out


def _urllib_download(url: str, dest: Path) -> None:  # pragma: no cover - network
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)


def fetch_one(model: Model, models_dir: str | Path, allow_unverified: bool = False,
              downloader: Callable[[str, Path], None] = _urllib_download) -> Action:
    """Fetch + verify one model. Raises ValueError on refusal or checksum mismatch (and removes a
    corrupt download). Idempotent: a verified file short-circuits with no network call."""
    dest_dir = Path(models_dir)
    dest = dest_dir / model.file
    status = classify(model, dest_dir)
    if status == "verified":
        return Action(model.id, OK, "already present and verified")
    if status == "missing" and not model.sha256 and not allow_unverified:
        raise ValueError(f"{model.id}: no sha256 pinned; refusing to download unverified "
                         f"(pass allow_unverified=True to override)")
    if not model.source:
        raise ValueError(f"{model.id}: no source URL in the catalog")

    dest_dir.mkdir(parents=True, exist_ok=True)
    downloader(model.source, dest)

    if model.sha256:
        got = sha256_file(dest)
        if got != model.sha256:
            dest.unlink(missing_ok=True)     # never leave corrupt weights on disk
            raise ValueError(f"{model.id}: checksum mismatch (got {got[:12]}…, "
                             f"expected {model.sha256[:12]}…) — download deleted")
        return Action(model.id, DOWNLOAD, "downloaded and checksum-verified")
    return Action(model.id, UNVERIFIED, "downloaded (UNVERIFIED — no sha256 pinned)")
