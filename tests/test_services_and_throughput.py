"""Tests for /api/services, /api/throughput/*, and the global exception handler."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import dashboard.app as dashboard_app

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)
    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", False)
    return TestClient(dashboard_app.app)


# ── /api/services ────────────────────────────────────────────────────────────

def test_services_returns_all_services(client):
    r = client.get("/api/services")
    assert r.status_code == 200
    data = r.json()
    assert "services" in data
    services = data["services"]
    assert len(services) >= 7
    ids = [s["id"] for s in services]
    assert "llamacpp" in ids
    assert "model-gateway" in ids


def test_services_have_required_fields(client):
    r = client.get("/api/services")
    for svc in r.json()["services"]:
        assert "id" in svc
        assert "name" in svc
        assert "port" in svc
        assert "ok" in svc
        assert "hint" in svc


def test_background_flag_flows_through_services_endpoint(client, monkeypatch):
    """The additive `background` key reaches the frontend via /api/services — routes_hub
    spreads the catalog entry (minus `check`), so it must not strip it. `background` now
    marks every NON-user-facing service (backend infra + headless workers), which the
    frontend splits into its 'Background jobs' section. Force the manifest fail-open path
    so the plugin-gated voice/rag cards are present."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    r = client.get("/api/services")
    by_id = {s["id"]: s for s in r.json()["services"]}
    for bg_id in ("rag-ingestion", "llamacpp", "mcp", "qdrant", "stt", "tts"):
        assert by_id[bg_id]["background"] is True, f"{bg_id} should be background"
    # User-facing UIs (main grid) never carry a truthy background flag.
    for ui_id in ("webui", "comfyui", "n8n", "hermes", "codebase-memory-ui", "model-gateway"):
        assert not by_id[ui_id].get("background"), f"{ui_id} must stay user-facing"


def test_headless_worker_shows_true_container_health(client, monkeypatch):
    """check:None background workers (rag-ingestion/livesync-bridge) have no HTTP
    endpoint to probe. The grid derives their up/down from ops-api container state+health
    rather than showing a neutral 'unknown': running+healthy -> ok True; not-running -> ok
    False (with a reason)."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)  # fail open so the headless cards show

    async def _fake_ops(method, path, *a, **k):
        assert path == "/services"
        return 200, {"services": [
            {"id": "rag-ingestion", "state": "running", "health": "healthy"},
            {"id": "livesync-bridge", "state": "exited", "health": None},
        ]}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    r = client.get("/api/services")
    by_id = {s["id"]: s for s in r.json()["services"]}
    assert by_id["rag-ingestion"]["ok"] is True
    assert by_id["livesync-bridge"]["ok"] is False
    assert by_id["livesync-bridge"]["error"], "a down worker must carry a container-state reason"


def test_hermes_health_comes_from_container_state_under_its_compose_name(client, monkeypatch):
    """The hermes card has no HTTP `check` and must resolve its container row through
    OPS_SERVICE_MAP (`hermes` -> `hermes-dashboard`).

    Regression: hermes-dashboard moved into caddy's netns and binds 127.0.0.1, so
    `hermes-dashboard:9119` stopped resolving on ordo-net and the old HTTP probe could only
    ever fail — a healthy Hermes rendered as down. Dropping the probe alone is not enough:
    ops-api keys /services by COMPOSE service name, so a lookup on the raw card id `hermes`
    misses and the card goes permanently grey instead of green."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)

    async def _fake_ops(method, path, *a, **k):
        assert path == "/services"
        return 200, {"services": [
            {"id": "hermes-dashboard", "state": "running", "health": "healthy"},
        ]}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    hermes = {s["id"]: s for s in client.get("/api/services").json()["services"]}["hermes"]
    assert hermes["ok"] is True, "hermes must read healthy from its compose-named container row"
    assert hermes.get("error") is None
    assert "check" not in hermes, "the unreachable HTTP probe must not come back"


def test_hermes_card_reports_down_when_its_container_is_down(client, monkeypatch):
    """The container-state path must still be able to say NO — otherwise dropping the HTTP
    check would have traded a false-red for a permanent false-green."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)

    async def _fake_ops(method, path, *a, **k):
        return 200, {"services": [
            {"id": "hermes-dashboard", "state": "exited", "health": None},
        ]}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    hermes = {s["id"]: s for s in client.get("/api/services").json()["services"]}["hermes"]
    assert hermes["ok"] is False
    assert hermes["error"], "a down hermes must carry a container-state reason"


def test_model_gateway_open_url_prefers_its_subdomain(client, monkeypatch):
    """model-gateway stays in the main grid (user-facing) and its Open link is the
    llm.<domain> subdomain — the same shape as every other service.

    It must NOT be the edge's `/llm/` route. That route strips its prefix, and LiteLLM's
    swagger HTML then requests root-absolute `/swagger/*` assets, which escape the handler
    and 404: the document returns 200 and the page renders blank. `/llm/*` stays the
    SSO-bypassing API base for programmatic bearer clients, not a browser entry."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    monkeypatch.setenv("CADDY_TAILNET_HOSTNAME", "ordo.example.ts.net")
    monkeypatch.setenv("CADDY_TAILNET_DOMAIN", "example.ts.net")
    monkeypatch.setenv("TAILNET_NAMES_ENABLED", "1")
    r = client.get("/api/services")
    mg = {s["id"]: s for s in r.json()["services"]}["model-gateway"]
    assert not mg.get("background")
    assert mg["open_url"] == "https://llm.example.ts.net/"
    assert "/llm/" not in mg["open_url"]


def test_model_gateway_open_url_falls_back_to_port_without_sidecars(client, monkeypatch):
    """With the sidecar layer disabled there is no subdomain, so the Open link falls back to
    the SSO'd port ROOT (:8449) — still a root, never the prefix-stripped /llm/ route."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    monkeypatch.delenv("TAILNET_NAMES_ENABLED", raising=False)
    monkeypatch.setenv("CADDY_TAILNET_HOSTNAME", "ordo.example.ts.net")
    r = client.get("/api/services")
    mg = {s["id"]: s for s in r.json()["services"]}["model-gateway"]
    assert mg["open_url"] == "https://ordo.example.ts.net:8449/"


def test_model_gateway_open_url_falls_back_when_host_unset(client, monkeypatch):
    """With no edge host known, model-gateway's open_url is None so the frontend falls back
    to its port/SSO route rather than emitting a broken link."""
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    monkeypatch.delenv("CADDY_TAILNET_HOSTNAME", raising=False)
    r = client.get("/api/services")
    mg = {s["id"]: s for s in r.json()["services"]}["model-gateway"]
    assert mg["open_url"] is None


def test_services_do_not_leak_auth_token(client, monkeypatch):
    """Regression: sensitive auth tokens must not appear in public /api/services URLs."""
    monkeypatch.setattr("dashboard.settings.DASHBOARD_AUTH_TOKEN", "secret-test-token-1234")
    # Re-import to pick up monkeypatched value
    import importlib

    import dashboard.services_catalog
    importlib.reload(dashboard.services_catalog)
    try:
        for svc in dashboard.services_catalog.SERVICES:
            assert "secret-test-token-1234" not in svc.get("url", ""), \
                f"Token leaked in service {svc['id']} URL: {svc['url']}"
    finally:
        importlib.reload(dashboard.services_catalog)


# ── /api/throughput/record ───────────────────────────────────────────────────

def test_throughput_record_accepts_sample(client):
    r = client.post("/api/throughput/record", json={
        "model": "test-model",
        "output_tokens_per_sec": 25.5,
        "service": "test-svc",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_throughput_record_ignores_zero_tps(client):
    r = client.post("/api/throughput/record", json={
        "model": "test-model",
        "output_tokens_per_sec": 0,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_throughput_record_ignores_empty_model(client):
    r = client.post("/api/throughput/record", json={
        "model": "",
        "output_tokens_per_sec": 10.0,
    })
    assert r.status_code == 200


# ── /api/throughput/stats ────────────────────────────────────────────────────

def test_throughput_stats_returns_models(client):
    # Seed a sample first
    client.post("/api/throughput/record", json={
        "model": "stats-test-model",
        "output_tokens_per_sec": 30.0,
        "ttft_ms": 120.0,
    })
    r = client.get("/api/throughput/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "models" in data
    # The seeded model should appear
    if "stats-test-model" in data["models"]:
        m = data["models"]["stats-test-model"]
        assert "latest" in m
        assert "peak" in m
        assert "p50" in m
        assert "p95" in m
        assert "sample_count" in m
        assert m["sample_count"] >= 1


# ── /api/throughput/service-usage ────────────────────────────────────────────

def test_throughput_service_usage_returns_by_model(client):
    client.post("/api/throughput/record", json={
        "model": "usage-test",
        "output_tokens_per_sec": 20.0,
        "service": "open-webui",
    })
    r = client.get("/api/throughput/service-usage")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "by_model" in data


# ── /api/auth/config ─────────────────────────────────────────────────────────

def test_auth_config_no_auth(client):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    data = r.json()
    assert data["auth_required"] is False


# ── Global exception handler ────────────────────────────────────────────────

def test_unhandled_exception_returns_500_not_traceback(monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", False)
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

    # Patch the GGUF disk scan (a dependency of /api/llm/models) to raise an
    # unexpected error. It is called without a try/except in the route, so the
    # error bubbles all the way to the global exception handler.
    def _boom():
        raise RuntimeError("test boom")

    monkeypatch.setattr("dashboard.app._scan_gguf_models", _boom)

    tc = TestClient(dashboard_app.app, raise_server_exceptions=False)
    r = tc.get("/api/llm/models")
    assert r.status_code == 500
    data = r.json()
    assert data["detail"] == "Internal server error"
    # Must NOT contain the traceback
    assert "test boom" not in str(data)


# ── Static app-shell caching ─────────────────────────────────────────────────

def test_index_html_sends_no_cache(client):
    """The HTML app shell must revalidate every load, so a rebuilt dashboard
    (new SSO routes / service cards) is picked up without a hard refresh."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert r.headers.get("cache-control") == "no-cache"
    # Still a validated cache — the ETag is what the browser revalidates against.
    assert r.headers.get("etag")
