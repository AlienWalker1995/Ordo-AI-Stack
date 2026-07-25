"""Build-context identity is a DECLARED, testable property (audit §2.1).

Every PROJECT-built image (`ordo/*` or `ordo-ai-stack-*`) referenced by ANY manifest OR by the
hardcoded substrate services must resolve — through the single `ordo.buildspec` resolver — to an
EXISTING build context + Dockerfile under `services/` (or be explicitly declared built out-of-band
via `build: {external: true}`). So a folder rename or an image typo fails CI, not deploy.

Pull-only UPSTREAM images (caddy, qdrant, n8n, node/llama.cpp bases, …) are exempt by construction:
the resolver returns None for them (they are not project images), and this test only asserts over
images the resolver classifies as project-built.
"""
from pathlib import Path

from ordo import buildspec
from ordo.agents import AgentRegistry
from ordo.compose import SUBSTRATE_BUILD_CONTEXTS
from ordo.dashboards import DashboardRegistry
from ordo.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

PLUGINS = PluginRegistry.load(SERVICES)
AGENTS = AgentRegistry.load(SERVICES)
DASHBOARDS = DashboardRegistry.load(SERVICES)
RESOLVE = buildspec.context_resolver(PLUGINS, AGENTS, DASHBOARDS, project="ordo")


def _all_project_images() -> set[str]:
    """Every project image any manifest declares + the substrate service images."""
    imgs: set[str] = set()
    for p in PLUGINS.plugins:
        imgs.update(buildspec._plugin_images(p))
    for a in AGENTS.agents:
        imgs.add(a.image_for("ordo"))
    for d in DASHBOARDS.dashboards:
        imgs.add(d.image_for("ordo"))
        if d.backend and d.backend.name:
            imgs.add(d.backend.image_for("ordo"))
    # substrate images (hardcoded in compose.py, no manifest): the project-namespaced core services
    # + the patched llama.cpp build referenced via a model's catalog backend_image.
    for name in SUBSTRATE_BUILD_CONTEXTS:
        imgs.add(f"ordo/{name}:latest")
    imgs.add("ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470")
    return {i for i in imgs if buildspec._is_project(i, "ordo")}


def test_every_project_image_resolves_to_a_context():
    unresolved = [i for i in _all_project_images() if RESOLVE(i) is None]
    assert not unresolved, f"project images with NO build context (rename/typo?): {unresolved}"


def test_every_resolved_context_has_a_dockerfile():
    """A real (non-external) context must point at an existing Dockerfile — so renaming a folder
    without updating the manifest/substrate map (or an image typo) fails here, not at deploy."""
    missing = []
    for img in sorted(_all_project_images()):
        ctx = RESOLVE(img)
        if ctx is None or ctx == buildspec.EXTERNAL:
            continue
        dockerfile = ROOT / ctx / "Dockerfile"
        if not dockerfile.is_file():
            missing.append(f"{img} -> {ctx}/Dockerfile (absent)")
    assert not missing, f"project images whose build context has no Dockerfile: {missing}"


def test_substrate_map_contexts_all_exist():
    """The hardcoded substrate map must not drift from the filesystem."""
    missing = [ctx for ctx in SUBSTRATE_BUILD_CONTEXTS.values()
               if not (ROOT / ctx / "Dockerfile").is_file()]
    assert not missing, f"SUBSTRATE_BUILD_CONTEXTS point at folders with no Dockerfile: {missing}"


def test_folder_id_differs_from_image_name_resolves_to_folder():
    """The 5 services where folder-id ≠ image-name (audit §2.1) must resolve to their real folder,
    not the (nonexistent) image-named one — the exact bug the resolver cures."""
    expected = {
        "ordo/mcpvault-mcp:latest": "services/memory-vault",
        "ordo/rag-ingestion:latest": "services/rag",
        "ordo/codebase-memory-mcp:latest": "services/codebase-memory",
        "ordo/orchestration-mcp:latest": "services/orchestration",
        "ordo/qdrant-rag-mcp:latest": "services/qdrant-rag",
    }
    for img, ctx in expected.items():
        assert RESOLVE(img) == ctx, f"{img} resolved to {RESOLVE(img)!r}, expected {ctx!r}"


def test_external_agents_declared_out_of_band():
    """Pluggable agent images with no in-repo Dockerfile must be declared external (not silently
    treated as pullable) — so a NEW agent lacking both a Dockerfile and build.external fails CI."""
    assert RESOLVE("ordo/agent-hermes:latest") == buildspec.EXTERNAL
    assert RESOLVE("ordo/agent-openai-agent:latest") == buildspec.EXTERNAL


def test_patched_llamacpp_resolves_via_substrate_not_substring():
    """The patched llama.cpp build (image name has NO `ordo/` prefix) resolves through the substrate
    map — the generic replacement for the deleted `'llamacpp-patched' in image` special-case."""
    assert RESOLVE("ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470") == "services/llamacpp-patched"


def test_build_field_is_not_rendered_into_compose():
    """`build:` is METADATA — it must never leak into a rendered compose service (which is image-only)."""
    from ordo.catalog import Catalog
    from ordo.config import Source
    from ordo.render import render
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    cat = Catalog.load(ROOT / "catalog" / "models.yaml")
    rc = render(src, cat, PLUGINS, agents=AGENTS, dashboards=DASHBOARDS)
    for name, svc in rc.compose_dict(project="ordo")["services"].items():
        assert "build" not in svc, f"service {name!r} leaked a build: key into compose"
