"""Ship repo-owned Hermes skills from the image, and keep them the ones Hermes actually loads.

Why this exists. A skill that lives only in the agent's home volume has no version control: a
volume loss deletes it, and Hermes rewrites its own SKILL.md files, so a safety rule written there
can quietly disappear. Skills whose correctness matters are therefore built into the image under
SHIPPED_ROOT (root-owned, read-only) and registered as a Hermes `skills.external_dirs` entry.
Hermes treats external dirs as externally owned: its curator will not rewrite them and its bundled
skill sync defers to them instead of writing a local copy.

Two entry points:

  build <bundled_skill_dir> <overlay_markdown> <dest_dir>
      Image build, as root. Copies an upstream bundled skill (already patched by the Dockerfile)
      to dest_dir and splices the overlay markdown into its SKILL.md before the first `## `
      heading, so the deployment notes are read before the upstream quick start. Fails the build
      if SKILL.md has no such heading, rather than guessing where the notes should go.

  boot
      Container start, as the hermes user, before the gateway starts. Registers SHIPPED_ROOT in
      config.yaml, then moves aside any skill in the local skills dir that has the same
      frontmatter name as a shipped one. Hermes lists local skills first and silently skips an
      external skill whose name is already taken, so a local copy would otherwise shadow the
      shipped one. Moved aside, never deleted: it goes to $HERMES_HOME/ordo-shadowed-skills/.
      Never blocks boot: every failure is logged and the script exits 0. Serialised with a lock
      file in $HERMES_HOME, because every container that shares the home volume runs it, so
      look for its log lines in whichever of them started first.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
from pathlib import Path

SHIPPED_ROOT = Path("/opt/ordo-skills")
SHADOW_ARCHIVE_DIRNAME = "ordo-shadowed-skills"


def log(message: str) -> None:
    print(f"[ordo-skills] {message}", flush=True)


# ---------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------

def insert_before_first_h2(skill_md: str, block: str) -> str:
    """Return skill_md with block inserted immediately before its first `## ` heading.

    Only headings outside the YAML frontmatter and outside fenced code blocks count.
    """
    lines = skill_md.splitlines(keepends=True)
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            index += 1
        index += 1  # step past the closing ---
    in_fence = False
    for position in range(index, len(lines)):
        stripped = lines[position].lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and stripped.startswith("## "):
            if not block.endswith("\n"):
                block += "\n"
            if not block.endswith("\n\n"):
                block += "\n"
            return "".join(lines[:position]) + block + "".join(lines[position:])
    raise ValueError("SKILL.md has no '## ' heading to insert the overlay before")


def build(bundled_skill_dir: Path, overlay_markdown: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        raise FileExistsError(f"{dest_dir} already exists; build writes a fresh copy only")
    shutil.copytree(
        bundled_skill_dir, dest_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    skill_md = dest_dir / "SKILL.md"
    original = skill_md.read_text(encoding="utf-8")
    block = overlay_markdown.read_text(encoding="utf-8")
    skill_md.write_text(insert_before_first_h2(original, block), encoding="utf-8")
    log(f"built {dest_dir} from {bundled_skill_dir} + {overlay_markdown.name}")


# ---------------------------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------------------------

def merged_external_dirs(current, required: str) -> list[str] | None:
    """The external_dirs list with `required` present, or None when nothing needs to change.

    Keeps every entry the operator added. A bare string (Hermes accepts one) becomes a list.
    """
    if current is None:
        entries: list[str] = []
    elif isinstance(current, str):
        entries = [current] if current.strip() else []
    elif isinstance(current, list):
        entries = [str(item) for item in current]
    else:
        entries = []
    if required in entries:
        return None if isinstance(current, list) else entries
    return entries + [required]


def frontmatter_name(skill_md: Path) -> str | None:
    """The `name:` from a SKILL.md frontmatter, or None if there is no readable one."""
    import yaml

    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:end])) or {}
            except yaml.YAMLError:
                return None
            name = data.get("name") if isinstance(data, dict) else None
            return str(name) if name else None
    return None


def skill_files(root: Path):
    """Every SKILL.md under root, skipping dot-directories (Hermes's own bookkeeping)."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def find_shadows(local_root: Path, shipped_root: Path) -> list[Path]:
    """Local skill directories whose frontmatter name matches a shipped skill."""
    shipped = {name for f in skill_files(shipped_root) if (name := frontmatter_name(f))}
    shadows = []
    for skill_md in skill_files(local_root):
        if frontmatter_name(skill_md) in shipped:
            shadows.append(skill_md.parent)
    return shadows


def move_aside(skill_dir: Path, local_root: Path, archive_root: Path, stamp: str) -> Path:
    destination = archive_root / stamp / skill_dir.relative_to(local_root)
    suffix = 1
    while destination.exists():
        destination = destination.with_name(f"{destination.name}.{suffix}")
        suffix += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_dir), str(destination))
    return destination


def register_shipped_root() -> None:
    from hermes_cli.config import load_config, save_config

    config = load_config()
    skills = config.setdefault("skills", {})
    updated = merged_external_dirs(skills.get("external_dirs"), str(SHIPPED_ROOT))
    if updated is None:
        return
    skills["external_dirs"] = updated
    save_config(config)
    log(f"registered {SHIPPED_ROOT} in skills.external_dirs")


def boot() -> None:
    if not SHIPPED_ROOT.is_dir():
        log(f"{SHIPPED_ROOT} not present in this image; nothing to do")
        return
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

    # More than one container runs this entrypoint against the same home volume (the agent and
    # the Hermes dashboard both do, and start within milliseconds of each other). Without a lock
    # both find the same shadow and race to move it, and both rewrite config.yaml. Serialise: the
    # first does the work, the second finds nothing left to do. The lock is released when the
    # process exits, so a crashed holder cannot wedge the next start.
    import fcntl  # Linux only; imported here so the module stays importable for tests elsewhere

    with open(hermes_home / ".ordo-skills.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _boot_locked(hermes_home)


def _boot_locked(hermes_home: Path) -> None:
    try:
        register_shipped_root()
    except Exception as exc:  # never block boot
        log(f"WARNING could not register {SHIPPED_ROOT} in config.yaml: {exc}")

    local_root = hermes_home / "skills"
    archive_root = hermes_home / SHADOW_ARCHIVE_DIRNAME
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        shadows = find_shadows(local_root, SHIPPED_ROOT)
    except Exception as exc:  # never block boot
        log(f"WARNING shadow check failed: {exc}")
        return
    for shadow in shadows:
        try:
            moved_to = move_aside(shadow, local_root, archive_root, stamp)
        except Exception as exc:  # one bad move must not stop the others
            log(f"WARNING could not move {shadow} aside: {exc}")
            continue
        log(f"moved local {shadow.relative_to(local_root)} aside to {moved_to}: "
            "it had the same name as a shipped skill and would have hidden it")


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "build":
        build(Path(argv[2]), Path(argv[3]), Path(argv[4]))
        return 0
    if len(argv) == 2 and argv[1] == "boot":
        try:
            boot()
        except Exception as exc:  # never block boot
            log(f"WARNING {exc}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
