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

Where the weights land: llama.cpp reads them from the `models-gguf` named volume (the 9p bind is
retired, see ordo/render/compose.py), so the default target is that volume, not a host directory. A
short-lived helper container (`HELPER_IMAGE`, digest-pinned) mounts ONLY the volume, downloads with
resume into a hidden `.<file>.part`, verifies the sha256 and renames the file into place, so a
service never sees a partial or unverified file. `ordo up` runs the same helper for every file the
services it starts read that the volume lacks (`ensure_models`). `--models-dir` keeps the host
directory download for the native (non-Docker) path.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shlex
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from ..render.catalog import Catalog, Model
from ..render.models_volume import (
    HELPER_IMAGE,
    MODEL_VOLUME,
    VOLUME_MOUNT,
    DockerRunner,
    files_in_volume,
    required_model_files,
    volume_exists,
    volume_name,
)
from .parity import load_env

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
    chosen = catalog.models if not wanted else [m for m in catalog.entries() if m.id in set(wanted)]
    models: list[Model] = []
    for chosen_model in chosen:
        for entry in catalog.files_of(chosen_model):      # the weights, then a pinned projector
            if entry not in models:
                models.append(entry)
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


# ── Fetching into the models-gguf volume (what llama.cpp actually reads) ──────
# The volume's identity, its listing and which files the stack reads: ordo/render/models_volume.py.

TOKEN_KEY = "HF_TOKEN"
# Labels on the helper containers: they say what a stray container is, and tell the runs apart.
FETCH_MARKER = "ordo.model-fetch=download"
EXIT_DOWNLOAD_FAILED = 2
EXIT_CHECKSUM_MISMATCH = 3

# The helper's program. Every input arrives as an environment variable (never interpolated into
# the script), and the token reaches curl through a header file, so it is in no argv.
HELPER_SCRIPT = r"""
set -eu
dir="${ORDO_FETCH_DIR:-/models}"
file="$ORDO_FETCH_FILE"
want="$ORDO_FETCH_SHA256"
dest="$dir/$file"
part="$dir/.$file.part"

# hash stdin, not a path: GNU sha256sum escapes some path names with a leading backslash
sha_of() { sha256sum < "$1" | cut -d ' ' -f 1; }

if [ -f "$dest" ]; then
  if [ -z "$want" ]; then
    echo "present (no sha256 pinned, not verified): $file"
    exit 0
  fi
  echo "verifying the existing $file"
  if [ "$(sha_of "$dest")" = "$want" ]; then
    echo "present and verified: $file"
    exit 0
  fi
  echo "$file does not match its pinned sha256: downloading a fresh copy"
fi

if [ -n "$want" ] && [ -f "$part" ] && [ "$(sha_of "$part")" = "$want" ]; then
  echo "a previous run already downloaded $file"
else
  echo "downloading $file"
  set -- --fail --location --show-error --progress-bar --retry 5 --retry-delay 10 \
    --continue-at - --output "$part"
  if [ -n "${HF_TOKEN:-}" ]; then
    header_file="$(mktemp)"
    trap 'rm -f "$header_file"' EXIT
    printf 'Authorization: Bearer %s\n' "$HF_TOKEN" > "$header_file"
    set -- "$@" --header "@$header_file"
  fi
  if ! curl "$@" "$ORDO_FETCH_URL"; then
    echo "download of $file failed; the partial download is kept, re-run to resume" >&2
    exit 2
  fi
fi

if [ -n "$want" ]; then
  echo "verifying $file"
  got="$(sha_of "$part")"
  if [ "$got" != "$want" ]; then
    rm -f "$part"
    echo "checksum mismatch for $file: got $got, expected $want; the download was deleted" >&2
    exit 3
  fi
fi
mv -f "$part" "$dest"
echo "installed: $file"
"""


def _check_file_name(file: str) -> None:
    """The file lands at <volume>/<file>: a plain name only, so nothing can leave the volume."""
    if not file or file.startswith(".") or "/" in file or "\\" in file:
        raise ValueError(f"refusing catalog file name {file!r}: it must be a plain file name")


def helper_argv(volume: str, model: Model) -> list[str]:
    """`docker run` for the download helper. The volume is its only mount, and HF_TOKEN is named
    (so docker copies the value from this process's environment) only for a gated model."""
    _check_file_name(model.file)
    argv = ["docker", "run", "--rm", "--label", FETCH_MARKER,
            # the image's default user cannot write the root-owned volume
            "--user", "0:0",
            "-v", f"{volume}:{VOLUME_MOUNT}",
            "-e", f"ORDO_FETCH_DIR={VOLUME_MOUNT}",
            "-e", f"ORDO_FETCH_FILE={model.file}",
            "-e", f"ORDO_FETCH_SHA256={model.sha256 or ''}",
            "-e", f"ORDO_FETCH_URL={model.source}"]
    if model.gated:
        argv += ["-e", TOKEN_KEY]
    return argv + ["--entrypoint", "sh", HELPER_IMAGE, "-c", HELPER_SCRIPT]


def create_volume(runner, volume: str, project: str) -> bool:
    """Create the volume with the labels compose gives its own, so `docker compose up` adopts it."""
    argv = ["docker", "volume", "create",
            "--label", f"com.docker.compose.project={project}",
            "--label", f"com.docker.compose.volume={MODEL_VOLUME}",
            volume]
    return runner.run(argv, capture=True).returncode == 0


def refusal(model: Model, allow_unverified: bool = False) -> str | None:
    """Why this entry cannot be downloaded into the volume, or None when it can."""
    # A file URL, not a repo or org page. Its name may differ from `file` (a projector is stored
    # under a model-specific name), but it has to be the same kind of file.
    url = urlparse(model.source)
    url_name = PurePosixPath(url.path).name
    suffix = PurePosixPath(model.file).suffix
    if url.scheme != "https" or not suffix or not url_name.endswith(suffix):
        return f"{model.id}: the catalog source {model.source!r} is not a download URL for {model.file}"
    if not model.sha256 and not allow_unverified:
        return (f"{model.id}: no sha256 pinned; refusing to download unverified weights "
                f"(`ordo fetch {model.id} --allow-unverified` overrides)")
    return None


def fetch_into_volume(models: Sequence[Model], *, project: str, secrets: Mapping[str, str], runner,
                      process_env: Mapping[str, str] | None = None) -> int:
    """Run the helper for each model: download, verify, move into place. 0 when all are in place.

    Each run is idempotent (a verified file is left alone). The token reaches the helper through
    the child's environment only, and only for a gated model."""
    process_env = os.environ if process_env is None else process_env
    volume = volume_name(project)
    for model in models:
        env = None
        if model.gated:
            token = (secrets.get(TOKEN_KEY) or process_env.get(TOKEN_KEY) or "").strip()
            if not token:
                print(f"{model.id} is gated on Hugging Face: set {TOKEN_KEY} in secrets.env (a read token "
                      f"from https://huggingface.co/settings/tokens), then re-run", file=sys.stderr)
                return 1
            env = {**process_env, TOKEN_KEY: token}
        print(f"fetching {model.id} ({model.file}) into the {volume} volume", flush=True)
        code = runner.run(helper_argv(volume, model), env=env).returncode
        if code == EXIT_CHECKSUM_MISMATCH:
            print(f"{model.id}: checksum mismatch, the download does not match the pinned sha256 and was "
                  f"deleted. Check the catalog entry's source and sha256.", file=sys.stderr)
            return 1
        if code == EXIT_DOWNLOAD_FAILED:
            print(f"{model.id}: the download failed; re-run to resume it", file=sys.stderr)
            return 1
        if code != 0:
            print(f"{model.id}: the fetch helper exited {code}", file=sys.stderr)
            return 1
    return 0


def ensure_models(doc: dict, env: Mapping[str, str], services: Sequence[str], *, catalog: Catalog,
                  project: str, secrets: Mapping[str, str], runner, dry_run: bool,
                  process_env: Mapping[str, str] | None = None) -> int:
    """`ordo up`'s model step: fetch every file `services` load that the volume lacks.

    A present file is not re-hashed here (it got in through a verified move, or by hand); `ordo
    fetch` re-verifies. A missing required file with no pinned catalog source refuses the bring-up,
    because its service would crash-loop; a missing projector is only a note."""
    needed = required_model_files(doc, env, services)
    if not needed:
        return 0
    volume = volume_name(project)
    exists = volume_exists(runner, volume)
    present: set[str] = set()
    if exists:
        listed = files_in_volume(runner, volume)
        if listed is None:
            print(f"cannot list the {volume} volume (pass --no-fetch to skip this step)", file=sys.stderr)
            return 1
        present = listed
    missing = [need for need in needed if need.file not in present]

    todo: list[Model] = []
    problems: list[str] = []
    for need in missing:
        model = catalog.by_file(need.file)
        if model is None:
            if need.optional:
                print(f"note: {need.file} ({need.service}, optional) is not in the {volume} volume and has "
                      f"no catalog source; that feature stays off until it is copied in")
            else:
                problems.append(f"{need.file} ({need.service}) is not in the {volume} volume and no "
                                f"catalog entry pins a source for it")
            continue
        reason = refusal(model)
        if reason:
            problems.append(reason)
        elif model not in todo:
            todo.append(model)
    if problems:
        print("cannot provision the models this bring-up needs:\n  " + "\n  ".join(problems)
              + "\n(copy the file into the volume by hand, or pass --no-fetch to start without it)",
              file=sys.stderr)
        return 1
    if not todo:
        return 0
    if dry_run:
        for model in todo:
            print(f"would fetch {model.id} ({model.file}) into the {volume} volume:\n"
                  f"  $ {shlex.join(helper_argv(volume, model)[:-1])} '<fetch script>'")
        return 0
    if not exists and not create_volume(runner, volume, project):
        print(f"cannot create the {volume} volume", file=sys.stderr)
        return 1
    return fetch_into_volume(todo, project=project, secrets=secrets, runner=runner, process_env=process_env)


def ensure_models_for_render(out_dir: str | Path, doc: dict, services: Sequence[str], *, project: str,
                             catalog_path: str | Path, dry_run: bool) -> int:  # pragma: no cover - docker
    """`ensure_models` wired to the rendered stack in `out_dir` and the real docker CLI."""
    out = Path(out_dir)
    env = load_env(str(out / ".env")) if (out / ".env").exists() else {}
    secrets = load_env(str(out / "secrets.env")) if (out / "secrets.env").exists() else {}
    return ensure_models(doc, env, services, catalog=Catalog.load(catalog_path), project=project,
                         secrets=secrets, runner=DockerRunner(), dry_run=dry_run)
