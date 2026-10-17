"""Routes the dashboard no longer serves, and why each one had to go.

Every one of these either could not work on the v2 stack or worked in a way the next render
silently undid. Keeping them meant UI controls that look live and do nothing, or worse, appear
to succeed and then revert. The replacements are the catalog switch (/api/models/switch) and the
guarded delete (/api/models/delete).
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dashboard.app as dashboard_app  # noqa: E402
from dashboard.app import app  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard"

RETIRED = [
    # wrote LLAMACPP_MODEL into .env: reverted by the next render, skipped the catalog settings
    ("POST", "/api/active-model"),
    ("POST", "/api/llamacpp/switch"),
    # the old Model tab's passthrough; its v1 payload never matched the v2 contract
    ("POST", "/api/model-config"),
    ("GET", "/api/model-config"),
    # wrote Open WebUI's default into .env, which the render regenerates without it
    ("GET", "/api/config/default-model"),
    ("POST", "/api/config/default-model"),
    # 501 since the GGUF puller was never ported
    ("POST", "/api/llm/pull"),
    ("GET", "/api/llm/pull/status"),
    ("POST", "/api/llm/unload"),
    # replaced by the guarded /api/models/delete
    ("POST", "/api/llm/delete"),
    # GPU pins are set at render time; the control plane answers 410
    ("POST", "/api/gpu/assign"),
    ("POST", "/api/registry/models/{model_id}/assign-gpu"),
    ("POST", "/api/orchestration/registry/models/{model_id}/assign-gpu"),
    # the control plane has no registry define/delete/enable-by-.env any more
    ("POST", "/api/registry/models"),
    ("DELETE", "/api/registry/models/{model_id}"),
    ("POST", "/api/registry/models/{model_id}/enable"),
    ("POST", "/api/orchestration/registry/models"),
    ("POST", "/api/orchestration/registry/models/{model_id}/enable"),
    # pack pulls need a models.json this deployment does not have; downloads live on as MCP tools
    ("GET", "/api/comfyui/packs"),
    ("POST", "/api/comfyui/pull"),
    ("GET", "/api/comfyui/pull/status"),
    ("POST", "/api/models/download"),
    ("GET", "/api/models/download/status"),
    ("POST", "/api/models/pull"),
    ("GET", "/api/models/pull/status"),
    # the pre-React vanilla shell
    ("GET", "/legacy-index.html"),
    # backends of the retired 11-tab UI that nothing calls any more (the pages use /api/overview,
    # /api/services/table, /api/models and /api/perf/*)
    ("GET", "/api/registry/models"),
    ("GET", "/api/registry/gpus"),
    ("GET", "/api/registry/models/{model_id}"),
    ("GET", "/api/gpu/list"),
    ("GET", "/api/dependencies"),
    ("GET", "/api/llm/ps"),
    ("GET", "/api/ops/available"),
    ("GET", "/api/performance/summary"),
    ("GET", "/api/throughput/service-usage"),
    # no HTTP caller after the five-page rewrite: the pages read these in-process through
    # /api/overview and /api/models, or never used them (the bearer modal's auth probe)
    ("GET", "/api/services"),
    ("GET", "/api/hardware"),
    ("GET", "/api/rag/status"),
    ("GET", "/api/throughput/stats"),
    ("GET", "/api/llm/models"),
    ("GET", "/api/auth/config"),
    ("GET", "/api/orchestration/gpu"),
    ("GET", "/api/orchestration/registry/models/{model_id}"),
    ("GET", "/api/orchestration/workflows/{workflow_id}/versions/{version}"),
    # the media worker's job / publish / schedule queue, retired 2026-07-31; these answered 410 since
    ("POST", "/api/orchestration/run"),
    ("GET", "/api/orchestration/jobs"),
    ("GET", "/api/orchestration/jobs/{job_id}"),
    ("POST", "/api/orchestration/jobs/{job_id}/cancel"),
    ("POST", "/api/orchestration/publish/enqueue"),
    ("POST", "/api/orchestration/publish/callback"),
    ("GET", "/api/orchestration/publish/status"),
    ("POST", "/api/orchestration/schedules"),
    ("GET", "/api/orchestration/schedules"),
    ("PATCH", "/api/orchestration/schedules/{schedule_id}"),
    ("DELETE", "/api/orchestration/schedules/{schedule_id}"),
    # the Media page and the Settings drawer, removed when the dashboard became the orchestration
    # plane only: MCP servers are managed in the LiteLLM UI (or ordo.yaml's plugins: list), media
    # in ComfyUI itself. ComfyUI restart stays on its Services card.
    ("GET", "/api/media"),
    ("GET", "/api/media/view"),
    ("GET", "/api/comfyui/models"),
    ("DELETE", "/api/comfyui/models/{category}/{filename}"),
    ("POST", "/api/comfyui/install-node-requirements"),
    ("GET", "/api/mcp/servers"),
    ("GET", "/api/mcp/health"),
    ("POST", "/api/mcp/add"),
    ("POST", "/api/mcp/remove"),
    ("GET", "/api/orchestration/gpu/history"),
]


def _served():
    """(method, path) for every API route, read from the OpenAPI schema. Walking app.routes
    breaks across FastAPI versions: newer ones nest included routers instead of flattening them."""
    served = {("GET", "/legacy-index.html")} if TestClient(app).get("/legacy-index.html").status_code == 200 else set()
    for path, operations in app.openapi()["paths"].items():
        served.update((method.upper(), path) for method in operations)
    return served


@pytest.mark.parametrize("method, path", RETIRED, ids=[f"{m} {p}" for m, p in RETIRED])
def test_retired_route_is_not_served(method, path):
    assert (method, path) not in _served()


def test_the_replacements_are_served():
    served = _served()
    for route in [("POST", "/api/models/switch"), ("POST", "/api/models/delete"), ("GET", "/api/models"),
                  ("GET", "/api/overview"), ("GET", "/api/activity"), ("GET", "/api/services/table"),
                  ("GET", "/api/perf/series"), ("GET", "/api/perf/grafana")]:
        assert route in served, route


def test_dashboard_has_no_writer_of_the_operator_source():
    # ops-controller is the only writer of ordo.yaml: the private copy of the plugins editor and
    # the source path are gone
    assert not hasattr(dashboard_app, "_edit_plugins_list")
    assert not hasattr(dashboard_app, "ORDO_SOURCE_PATH")
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (DASHBOARD_DIR / "dashboard").glob("*.py"))
    assert "ORDO_SOURCE_PATH" not in sources and "/ordo-source.yaml" not in sources
    # and the dashboard does not mount out/ordo.yaml at all
    manifest = (DASHBOARD_DIR / "dashboard.yaml").read_text(encoding="utf-8")
    assert "out/ordo.yaml" not in manifest and "ORDO_SOURCE_PATH" not in manifest
