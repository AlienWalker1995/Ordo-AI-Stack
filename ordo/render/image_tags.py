"""The first-party image record and how render pins it: `out/images.json` and `image:` tags.

`ordo build` (ordo/host/images.py, which documents the whole tag model) records the tag of each
first-party image it builds in out/images.json. Every render, on the host and inside ops-controller,
reads that record and writes the recorded tag into each untagged first-party `image:`. The record
format and the pinning rule live here, in the render layer, because the render reads them; building
images is host work.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import buildspec
from .compose import SUBSTRATE_BUILD_CONTEXTS, SUBSTRATE_IMAGES

if TYPE_CHECKING:
    from .agents import AgentRegistry
    from .dashboards import DashboardRegistry
    from .plugins import PluginRegistry

RECORD_FILE = "images.json"
# The tag render uses for a first-party image `ordo build` has not recorded yet. `ordo build`
# moves it to every image it builds, so a render made before the first build still resolves.
FALLBACK_TAG = "current"
# Docker's tag grammar.
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


# --- the record (out/images.json) ---


def load_record(out_dir: str | Path) -> dict[str, str]:
    """`{image: tag}` from out/images.json; {} when the file does not exist.

    A present but unreadable record is an error, not an empty record: silently rendering `current`
    over a real record would move every service off the build it runs."""
    path = Path(out_dir) / RECORD_FILE
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {path} ({e}); fix or delete it, then run `ordo build`") from e
    recorded = doc.get("images") if isinstance(doc, dict) else None
    if not isinstance(recorded, dict):
        raise ValueError(f"{path} has no `images` map; fix or delete it, then run `ordo build`")
    for image, tag in recorded.items():
        if not isinstance(image, str) or not isinstance(tag, str) or not _TAG_RE.match(tag):
            raise ValueError(f"{path}: {image!r} has an invalid tag {tag!r}")
    return dict(recorded)


def save_record(out_dir: str | Path, record: dict[str, str]) -> None:
    """Write the record atomically, so a render never reads half a file."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RECORD_FILE
    tmp = path.with_name(RECORD_FILE + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "images": dict(sorted(record.items()))}, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# --- which images are first-party ---


def has_tag(ref: str) -> bool:
    """True when the ref names its own tag or digest (`repo:tag`, `repo@sha256:...`)."""
    return "@" in ref or ":" in ref.rsplit("/", 1)[-1]


def _declares_own_version(ref: str) -> bool:
    """A declaration render must leave alone: a tag, a digest, or a `${VAR:-default}` override."""
    return ref.startswith("${") or has_tag(ref)


def first_party_contexts(plugins: PluginRegistry, agents: AgentRegistry, dashboards: DashboardRegistry,
                         *, project: str = "ordo") -> dict[str, str]:
    """`{image: build context}` for every image `ordo build` owns and render tags.

    That is every project image with an in-repo build context whose declaration carries no tag of
    its own: the substrate images (compose.py's, and the catalog's patched llama.cpp build), plus
    each manifest image built in the repo."""
    contexts = buildspec.manifest_image_contexts(plugins, agents, dashboards, project=project)
    declared = [a.image_for(project) for a in agents.agents]
    declared += [d.image_for(project) for d in dashboards.dashboards]
    declared += [str(ref) for p in plugins.plugins for ref in buildspec._plugin_images(p)]
    self_versioned = {buildspec.image_ident(ref) for ref in declared if _declares_own_version(ref)}
    first_party = {image: ctx for image, ctx in contexts.items()
                   if ctx != buildspec.EXTERNAL and image not in self_versioned}
    for name in SUBSTRATE_IMAGES:
        first_party[f"{project}/{name}"] = SUBSTRATE_BUILD_CONTEXTS[name]
    return first_party


def pin_first_party(services: dict[str, Any], first_party: Iterable[str], tags: dict[str, str]) -> None:
    """Give every untagged first-party `image:` its recorded tag (FALLBACK_TAG when unrecorded)."""
    owned = set(first_party)
    for spec in services.values():
        ref = str((spec or {}).get("image") or "")
        if not ref or _declares_own_version(ref) or ref not in owned:
            continue
        spec["image"] = f"{ref}:{tags.get(ref, FALLBACK_TAG)}"
