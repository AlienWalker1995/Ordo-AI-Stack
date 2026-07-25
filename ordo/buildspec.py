"""Build-context identity — the missing link between an `image:` and the folder it builds from.

The rendered compose is image-only (build is out-of-band), so historically the image→context map
lived only in per-service README prose plus one hardcoded preflight branch, and folder-id ≠
image-name for several services (e.g. `memory-vault` builds `ordo/mcpvault-mcp`). That is the
structural seam audit §2.1 calls out: a new special-case each time.

This module makes build-context a DECLARED, testable property:
  - `BuildSpec` is the optional `build:` block a plugin/agent/dashboard manifest may carry
    (context dir + dockerfile), defaulting to the service's own `services/<id>/` + `Dockerfile`.
    It is pure METADATA — never emitted into the rendered compose (render stays image-only).
  - `context_resolver()` returns a single image→context resolver covering BOTH manifest services
    (from their `build:`/default) AND the hardcoded substrate services (`SUBSTRATE_BUILD_CONTEXTS`
    in compose.py). Preflight uses it to emit a generic "build from <context>" hint for EVERY
    project image, and the substrate test uses it to assert every project image resolves to an
    existing Dockerfile (so a rename/typo fails CI, not deploy).
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .compose import SUBSTRATE_BUILD_CONTEXTS

if TYPE_CHECKING:
    from .agents import AgentRegistry
    from .dashboards import DashboardRegistry
    from .plugins import Plugin, PluginRegistry

# Sentinel context: a project-namespaced image that is genuinely built OUT-OF-BAND (no in-repo
# Dockerfile) — e.g. a pluggable agent image the operator/third-party builds from their own tree.
# Declared via `build: {external: true}` so "this image has no in-repo build context" is an
# explicit manifest property, not tribal knowledge. Buildable (Docker can't pull it) but exempt
# from the "must resolve to a Dockerfile" substrate test.
EXTERNAL = "<external>"


@dataclasses.dataclass(frozen=True)
class BuildSpec:
    """The optional `build:` block on a manifest. METADATA only — NEVER rendered into compose.

    Absent -> defaults to the service's own `services/<id>/` context + `Dockerfile`. Declare it
    explicitly only when the context isn't the service's own dir (e.g. a nested build context) or
    the image is built out-of-band (`external: true`)."""
    context: str = ""              # "" -> services/<id> (filled by context_for)
    dockerfile: str = "Dockerfile"
    external: bool = False         # image built out-of-band; no in-repo Dockerfile

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> BuildSpec:
        d = d or {}
        return cls(
            context=str(d.get("context", "") or ""),
            dockerfile=str(d.get("dockerfile", "") or "Dockerfile"),
            external=bool(d.get("external", False)),
        )

    def context_for(self, service_id: str) -> str:
        """The build-context dir (repo-root-relative), defaulting to the service's own folder."""
        return self.context or f"services/{service_id}"

    def resolved(self, service_id: str) -> str:
        """EXTERNAL for an out-of-band image, else the build-context dir."""
        return EXTERNAL if self.external else self.context_for(service_id)


def image_ident(image: str) -> str:
    """Strip a `${VAR:-default}` wrapper + registry tag/digest -> the `repo/name` identity used as
    the map key. Unwrapping the default makes the RAW manifest ref (`${LTX_TRAINER_IMAGE:-ordo/
    ltx-trainer:9377…}`) and the preflight-EXPANDED ref (`ordo/ltx-trainer:9377…`) resolve alike.

    `ordo/model-gateway:latest`               -> `ordo/model-gateway`
    `ordo-ai-stack-llamacpp-patched:qwen36…`  -> `ordo-ai-stack-llamacpp-patched`
    `${LTX_TRAINER_IMAGE:-ordo/ltx-trainer:9…}` -> `ordo/ltx-trainer`
    `ghcr.io/x/y@sha256:…`                     -> `ghcr.io/x/y`
    """
    img = str(image)
    if img.startswith("${") and ":-" in img:          # unwrap ${VAR:-default} to its default
        img = img.split(":-", 1)[1].rstrip("}")
    img = img.split("@", 1)[0]                         # drop @sha256:…
    if ":" in img.rsplit("/", 1)[-1]:                 # a :tag on the final path segment
        img = img.rsplit(":", 1)[0]
    return img


def substrate_context(image: str) -> str | None:
    """Build context for a hardcoded substrate image, else None. Matches on the `repo/name` or the
    `…-<name>` suffix so both `ordo/model-gateway` and `ordo-ai-stack-llamacpp-patched` resolve."""
    ident = image_ident(image)
    for name, ctx in SUBSTRATE_BUILD_CONTEXTS.items():
        if ident == name or ident.endswith("/" + name) or ident.endswith("-" + name):
            return ctx
    return None


def _is_project(image: str, project: str) -> bool:
    """A repo-built image: project-namespaced (`<project>/*`) or a known substrate build."""
    return image_ident(image).startswith(f"{project}/") or substrate_context(image) is not None


def _plugin_images(p: Plugin) -> list[str]:
    """Every image a plugin declares (kind=mcp `mcp.image` + each kind=service `services[].image`)."""
    imgs: list[str] = []
    mcp_img = str((p.mcp or {}).get("image", "") or "")
    if mcp_img:
        imgs.append(mcp_img)
    imgs.extend(str(s.image) for s in p.services if s.image)
    return imgs


def manifest_image_contexts(
    plugins: PluginRegistry, agents: AgentRegistry, dashboards: DashboardRegistry, *, project: str,
) -> dict[str, str]:
    """Map every PROJECT image a manifest builds -> its build context (from `build:` or the
    `services/<id>` default), or EXTERNAL for an out-of-band image.

    First writer wins, in canonical-owner order (agents, then dashboards, then plugins): the
    `ordo/agent-<id>` image is OWNED by the agent, so the hermes-dashboard plugin that merely
    REUSES that image doesn't mis-claim it under its own (Dockerfile-less) folder."""
    m: dict[str, str] = {}

    def claim(image: str, ctx: str) -> None:
        if _is_project(image, project):
            m.setdefault(image_ident(image), ctx)

    for a in agents.agents:
        claim(a.image_for(project), a.build.resolved(a.id))
    for d in dashboards.dashboards:
        claim(d.image_for(project), d.build.resolved(d.id))
        if d.backend and d.backend.name:
            claim(d.backend.image_for(project), d.backend.build.resolved(d.backend.name))
    for p in plugins.plugins:
        for img in _plugin_images(p):
            claim(img, p.build.resolved(p.id))
    return m


def context_resolver(
    plugins: PluginRegistry, agents: AgentRegistry, dashboards: DashboardRegistry, *, project: str,
) -> Callable[[str], str | None]:
    """A single image->context resolver over substrate + manifests. Returns:
      - a build-context dir  (repo-root-relative) for a repo-built project image,
      - EXTERNAL             for a project image built out-of-band (no in-repo Dockerfile),
      - None                 for an upstream image Docker pulls (not a project image).
    Substrate wins over manifests (the 4 hardcoded core-service images are unambiguous)."""
    mmap = manifest_image_contexts(plugins, agents, dashboards, project=project)

    def resolve(image: str) -> str | None:
        sc = substrate_context(image)
        if sc is not None:
            return sc
        return mmap.get(image_ident(image))

    return resolve
