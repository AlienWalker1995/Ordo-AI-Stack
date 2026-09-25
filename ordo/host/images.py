"""First-party image identity: which commit each `ordo/<name>` image was built from.

The rendered compose is image-only, so the stack's own images (the substrate services, the
dashboard, the agent, the MCP adapters) are built out of band. They used to float on
`ordo/<name>:latest`: nothing said which commit a container ran, and a rollback was a hand retag.

The model, in one place:

- `ordo build` tags each first-party image `ordo/<name>:<commit>`, where `<commit>` is the short
  sha of the last commit that changed the image's build inputs (its build context; for the one
  image built from the repo root, the paths the root `.dockerignore` lets in). Uncommitted changes
  to those inputs add `-dirty`. It also moves `ordo/<name>:current` to the new tag.
- Each tag `ordo build` produces is recorded in `out/images.json` (RECORD_FILE). That record is
  the one source of image tags.
- Every render writes the recorded tag straight into the compose `image:` field (or `current` for
  an image with no record yet). The host render and ops-controller's in-container render both
  write into the same out/ directory (mounted at /config), so both read the same record, and
  `docker inspect` on a container names the commit.
- Because a tag only moves when an image's own inputs change, a deploy (`ordo build`, render,
  `ordo up`) recreates only the services whose image actually changed.

Manifests and compose.py therefore declare first-party images UNTAGGED (`ordo/dashboard`): the
tag is render's to fill; so does a model's catalog `backend_image` (the patched llama.cpp build).
An image declared with its own tag (ltx-trainer) or built out of band (`build: {external: true}`)
is not first-party here and is never retagged.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..render import buildspec
from ..render.agents import AgentRegistry
from ..render.dashboards import DashboardRegistry
from ..render.engine import DEFAULT_AGENTS_DIR, DEFAULT_DASHBOARDS_DIR, DEFAULT_PLUGINS_DIR
from ..render.image_tags import FALLBACK_TAG, RECORD_FILE, first_party_contexts, load_record, save_record
from ..render.plugins import PluginRegistry
from ..render.stack import COMPOSE_FILE, load_compose

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION_LABEL = "org.opencontainers.image.revision"
SHORT_SHA_LENGTH = 12
# The images built with the repo ROOT as their context (ops-controller ships ordo/, catalog/ and
# services/). Their identity is the path set the root .dockerignore allowlists.
ROOT_CONTEXT_IMAGES = frozenset({"ordo/ops-controller"})


def select_images(doc: dict[str, Any], first_party: dict[str, str], services: Sequence[str] | None) -> list[str]:
    """The first-party images the rendered compose runs, for the named services or (None) all."""
    rendered = doc.get("services") or {}
    names = list(rendered) if services is None else list(services)
    selected: set[str] = set()
    for name in names:
        if name not in rendered:
            raise ValueError(f"no such service in the rendered stack: {name}")
        ref = str((rendered[name] or {}).get("image") or "")
        image = buildspec.image_ident(ref)
        if ref.startswith("${") or image not in first_party:
            if services is None:
                continue
            raise ValueError(f"{name} runs {ref or '(no image)'}, which is not a first-party image "
                             "`ordo build` manages (upstream, pinned, or built out of band)")
        selected.add(image)
    return sorted(selected)


# --- build targets and their identity ---


@dataclasses.dataclass(frozen=True)
class BuildTarget:
    image: str                   # `ordo/<name>`, no tag
    dockerfile: str              # repo-relative
    context: str                 # repo-relative build context ("." for the repo root)
    inputs: tuple[str, ...]      # repo-relative paths whose git history identifies the image


def _dockerignore_allowlist(repo_root: Path) -> tuple[str, ...]:
    """The directories the root .dockerignore re-includes (`!ordo/` -> `ordo`)."""
    lines = (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return tuple(line[1:].strip().rstrip("/") for line in lines if line.startswith("!"))


def build_target(image: str, context: str, repo_root: Path = REPO_ROOT) -> BuildTarget:
    dockerfile = f"{context}/Dockerfile"
    if image in ROOT_CONTEXT_IMAGES:
        return BuildTarget(image, dockerfile, ".", _dockerignore_allowlist(repo_root))
    return BuildTarget(image, dockerfile, context, (context,))


class GitLike(Protocol):
    def last_commit(self, paths: Sequence[str]) -> str: ...
    def head(self) -> str: ...
    def is_dirty(self, paths: Sequence[str]) -> bool: ...


class DockerLike(Protocol):
    def image_exists(self, ref: str) -> bool: ...
    def build(self, target: BuildTarget, ref: str, labels: dict[str, str]) -> bool: ...
    def tag(self, source: str, target: str) -> bool: ...


class Git:
    """The three questions `ordo build` asks the checkout."""

    def __init__(self, repo_root: str | Path = REPO_ROOT):
        self.repo_root = Path(repo_root)

    def _git(self, *args: str) -> str:
        try:
            proc = subprocess.run(["git", "-C", str(self.repo_root), *args],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"`ordo build` needs git and a git checkout: {e}") from e
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def last_commit(self, paths: Sequence[str]) -> str:
        return self._git("log", "-1", "--format=%H", "--", *paths)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def is_dirty(self, paths: Sequence[str]) -> bool:
        return bool(self._git("status", "--porcelain", "--untracked-files=all", "--", *paths))


class Docker:
    """The docker CLI calls `ordo build` makes, run from the repo root."""

    def __init__(self, repo_root: str | Path = REPO_ROOT):
        self.repo_root = Path(repo_root)

    def _run(self, cmd: list[str], *, quiet: bool) -> bool:
        try:
            proc = subprocess.run(cmd, cwd=self.repo_root, capture_output=quiet)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  ! {' '.join(cmd[:3])} failed to run: {e}", file=sys.stderr)
            return False
        return proc.returncode == 0

    def image_exists(self, ref: str) -> bool:
        return self._run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], quiet=True)

    def build(self, target: BuildTarget, ref: str, labels: dict[str, str]) -> bool:
        cmd = ["docker", "build", "-f", target.dockerfile, "-t", ref]
        for key, value in labels.items():
            cmd += ["--label", f"{key}={value}"]
        print(f"  $ {' '.join(cmd + [target.context])}", flush=True)
        return self._run(cmd + [target.context], quiet=False)

    def tag(self, source: str, target: str) -> bool:
        return self._run(["docker", "tag", source, target], quiet=True)


def _identity(git: GitLike, inputs: Sequence[str]) -> tuple[str, str, bool]:
    """(commit, tag, dirty). Inputs never committed fall back to HEAD (and are dirty by definition)."""
    commit = git.last_commit(inputs) or git.head()
    dirty = git.is_dirty(inputs)
    tag = commit[:SHORT_SHA_LENGTH] + ("-dirty" if dirty else "")
    return commit, tag, dirty


@dataclasses.dataclass(frozen=True)
class PlannedBuild:
    """One target at its content tag, and whether `ordo build` has to build it."""
    target: BuildTarget
    commit: str
    tag: str
    dirty: bool
    exists: bool                 # the tag is already in the local image cache

    @property
    def ref(self) -> str:
        return f"{self.target.image}:{self.tag}"

    @property
    def needs_build(self) -> bool:
        """A clean tag that exists is not rebuilt; a `-dirty` tag names no single content, so it is."""
        return self.dirty or not self.exists


def plan_builds(targets: Sequence[BuildTarget], *, git: GitLike, docker: DockerLike) -> list[PlannedBuild]:
    """Each target's content tag and whether it needs building. Reads git and the image cache only."""
    planned = []
    for target in targets:
        commit, tag, dirty = _identity(git, target.inputs)
        exists = False if dirty else docker.image_exists(f"{target.image}:{tag}")
        planned.append(PlannedBuild(target=target, commit=commit, tag=tag, dirty=dirty, exists=exists))
    return planned


# --- building ---


def build_images(targets: Sequence[BuildTarget], *, git: GitLike, docker: DockerLike, out_dir: str | Path,
                 dry_run: bool = False) -> int:
    """Build (or confirm) each target at its content tag, move `:current`, and record the tag.

    Idempotent: a clean content tag that already exists is not rebuilt. A `-dirty` tag names no
    single content, so it is always rebuilt. Each success is recorded as it lands, so one failed
    image does not lose the others. Returns 0 when every target is available, else 1."""
    failed: list[str] = []
    for planned in plan_builds(targets, git=git, docker=docker):
        target, ref = planned.target, planned.ref
        if not planned.needs_build:
            action = "up to date"
        elif dry_run:
            print(f"  would build {ref}  (-f {target.dockerfile}, context {target.context})")
            continue
        elif docker.build(target, ref, {REVISION_LABEL: planned.commit}):
            action = "built"
        else:
            print(f"  FAILED {ref}", file=sys.stderr)
            failed.append(target.image)
            continue
        if dry_run:
            print(f"  {action:<10} {ref}")
            continue
        if not docker.tag(ref, f"{target.image}:{FALLBACK_TAG}"):
            print(f"  FAILED to tag {ref} as :{FALLBACK_TAG}", file=sys.stderr)
            failed.append(target.image)
            continue
        save_record(out_dir, {**load_record(out_dir), target.image: planned.tag})
        print(f"  {action:<10} {ref}")
    if failed:
        print(f"{len(failed)} image(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


BuildFn = Callable[..., int]


def ensure_built(doc: dict[str, Any], services: Iterable[str], *, first_party: dict[str, str],
                 repo_root: Path, out_dir: str | Path, docker: DockerLike, git: GitLike,
                 build: BuildFn | None = None, dry_run: bool = False) -> int:
    """Build the first-party images `services` run that the local daemon does not have.

    The rendered compose names exact tags. When the checkout builds a different tag than the one
    rendered (the compose predates a newer commit), the named image still does not exist after the
    build: refuse and ask for a re-render instead of starting on a missing image."""
    build = build or build_images
    rendered = doc.get("services") or {}
    wanted: dict[str, str] = {}
    for name in sorted(services):
        ref = str((rendered.get(name) or {}).get("image") or "")
        image = buildspec.image_ident(ref)
        if ref and not ref.startswith("${") and image in first_party:
            wanted[ref] = image
    missing = {ref: image for ref, image in wanted.items() if not docker.image_exists(ref)}
    if not missing:
        return 0
    print(f"building {len(missing)} missing first-party image(s): {', '.join(sorted(missing))}")
    targets = [build_target(image, first_party[image], repo_root) for image in sorted(set(missing.values()))]
    code = build(targets, git=git, docker=docker, out_dir=out_dir, dry_run=dry_run)
    if code or dry_run:
        return code
    still_missing = sorted(ref for ref in missing if not docker.image_exists(ref))
    if still_missing:
        print(f"the rendered compose names {', '.join(still_missing)}, which this checkout does not "
              "build (its inputs changed since that render). Re-render so the compose names the new "
              "build: ordo --source out/ordo.yaml render --out out", file=sys.stderr)
        return 1
    return 0


def _registries() -> tuple[PluginRegistry, AgentRegistry, DashboardRegistry]:
    return (PluginRegistry.load(DEFAULT_PLUGINS_DIR), AgentRegistry.load(DEFAULT_AGENTS_DIR),
            DashboardRegistry.load(DEFAULT_DASHBOARDS_DIR))


def ensure_images(out_dir: str | Path, doc: dict[str, Any], services: Iterable[str], *, project: str,
                  dry_run: bool) -> int:
    """`ordo up`'s build step, wired to the real checkout and docker daemon."""
    try:
        return ensure_built(doc, services, first_party=first_party_contexts(*_registries(), project=project),
                            repo_root=REPO_ROOT, out_dir=out_dir, docker=Docker(), git=Git(),
                            dry_run=dry_run)
    except (RuntimeError, ValueError) as e:
        print(f"ordo up: cannot build images: {e}", file=sys.stderr)
        return 1


def build_targets(doc: dict[str, Any], services: Sequence[str] | None, *, project: str,
                  skip_upstream: bool = False) -> list[BuildTarget]:
    """The build targets of the first-party images `doc` runs, for the named services or (None) all.

    Raises ValueError for a named service that is unknown or, unless `skip_upstream`, runs an image
    `ordo build` does not manage (with `skip_upstream` such a service simply has nothing to build)."""
    first_party = first_party_contexts(*_registries(), project=project)
    if services is not None and skip_upstream:
        rendered = doc.get("services") or {}
        services = [name for name in services if name not in rendered
                    or buildspec.image_ident(str((rendered[name] or {}).get("image") or "")) in first_party]
    return [build_target(image, first_party[image]) for image in select_images(doc, first_party, services)]


def run_build(out_dir: str | Path, services: Sequence[str] | None, *, project: str, dry_run: bool) -> int:
    """`ordo build`: build the first-party images the rendered compose in `out_dir` runs."""
    try:
        doc = load_compose(Path(out_dir).as_posix())
    except OSError as e:
        print(f"cannot read {out_dir}/{COMPOSE_FILE} ({e}); render first: "
              "ordo --source out/ordo.yaml render --out out", file=sys.stderr)
        return 1
    try:
        targets = build_targets(doc, services, project=project)
        code = build_images(targets, git=Git(), docker=Docker(), out_dir=out_dir, dry_run=dry_run)
    except (RuntimeError, ValueError) as e:
        print(f"ordo build: {e}", file=sys.stderr)
        return 1
    if code == 0 and not dry_run:
        print(f"recorded in {Path(out_dir) / RECORD_FILE}. Deploy them (render, then recreate exactly the "
              "services that changed): ordo apply")
    return code
