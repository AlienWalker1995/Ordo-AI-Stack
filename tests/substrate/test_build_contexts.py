"""Build-context identity is a DECLARED, testable property (audit §2.1).

Every PROJECT-built image (`ordo/*`) referenced by ANY manifest OR by the
hardcoded substrate services must resolve — through the single `ordo.render.buildspec` resolver — to an
EXISTING build context + Dockerfile under `services/` (or be explicitly declared built out-of-band
via `build: {external: true}`). So a folder rename or an image typo fails CI, not deploy.

Pull-only UPSTREAM images (caddy, qdrant, n8n, node/llama.cpp bases, …) are exempt by construction:
the resolver returns None for them (they are not project images), and this test only asserts over
images the resolver classifies as project-built.
"""
from pathlib import Path

from ordo.render import buildspec
from ordo.render.agents import AgentRegistry
from ordo.render.compose import SUBSTRATE_BUILD_CONTEXTS
from ordo.render.dashboards import DashboardRegistry
from ordo.render.plugins import PluginRegistry

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
    # substrate images (no manifest): the core services compose.py names + the patched llama.cpp
    # build a model's catalog backend_image names.
    for name in SUBSTRATE_BUILD_CONTEXTS:
        imgs.add(f"ordo/{name}")
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
    """The services where folder-id ≠ image-name (audit §2.1) must resolve to their real folder,
    not the (nonexistent) image-named one — the exact bug the resolver cures."""
    expected = {
        "ordo/n8n-mcp": "services/n8n",
        "ordo/mcpvault-mcp": "services/memory-vault",
        "ordo/rag-ingestion": "services/rag",
        "ordo/codebase-memory-mcp": "services/codebase-memory",
        "ordo/orchestration-mcp": "services/orchestration",
        "ordo/qdrant-rag-mcp": "services/qdrant-rag",
    }
    for img, ctx in expected.items():
        assert RESOLVE(img) == ctx, f"{img} resolved to {RESOLVE(img)!r}, expected {ctx!r}"


def test_hermes_resolves_to_its_in_repo_context():
    """agent-hermes has a co-located in-repo build context (services/hermes/ + Dockerfile), so it
    must resolve to that real folder — NOT external. Guards the reorg: a manifest that re-declares
    hermes external, or a move that strands its Dockerfile, fails here."""
    assert RESOLVE("ordo/agent-hermes") == "services/hermes"
    assert (ROOT / "services/hermes/Dockerfile").is_file()


def test_external_agents_declared_out_of_band():
    """Pluggable agent images with no in-repo Dockerfile must be declared external (not silently
    treated as pullable) — so a NEW agent lacking both a Dockerfile and build.external fails CI."""
    assert RESOLVE("ordo/agent-openai-agent:latest") == buildspec.EXTERNAL


def test_patched_llamacpp_resolves_via_substrate_not_substring():
    """The patched llama.cpp build (named by a catalog `backend_image`, not a manifest) resolves through
    the substrate map: the generic replacement for the deleted `'llamacpp-patched' in image` special-case."""
    assert RESOLVE("ordo/llamacpp-patched:0123456789ab") == "services/llamacpp-patched"


def test_build_field_is_not_rendered_into_compose():
    """`build:` is METADATA — it must never leak into a rendered compose service (which is image-only)."""
    from ordo.render.catalog import Catalog
    from ordo.render.config import Source
    from ordo.render.engine import render
    src = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                            "model": "auto", "plugins": "auto"})
    cat = Catalog.load(ROOT / "catalog" / "models.yaml")
    rc = render(src, cat, PLUGINS, agents=AGENTS, dashboards=DASHBOARDS)
    for name, svc in rc.compose_dict(project="ordo")["services"].items():
        assert "build" not in svc, f"service {name!r} leaked a build: key into compose"
