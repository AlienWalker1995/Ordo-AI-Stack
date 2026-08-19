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
    client.post("/api/throughput/record", json={
        "model": "stats-test-model",
        "output_tokens_per_sec": 30.0,
        "ttft_ms": 120.0,
    })
    r = client.get("/api/throughput/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    m = data["models"]["stats-test-model"]
    for key in ("latest", "peak", "p50", "p95", "sample_count", "last_ts", "first_ts"):
        assert key in m
    assert m["sample_count"] >= 1
    assert m["last_ts"] > 0
    assert m["first_ts"] <= m["last_ts"]


def test_throughput_stats_includes_active_model(client, monkeypatch):
    """The tab must never GUESS the active model — /stats carries the ops-controller's
    answer (the same authority Model Control uses)."""
    import dashboard.app as dashboard_app

    async def _fake_ops(method, path, *a, **k):
        assert (method, path) == ("GET", "/model-config")
        return 200, {"active_model": "Qwen-Test-Q6_K.gguf", "running": {}}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    monkeypatch.setattr(dashboard_app, "_active_model_cache", {"checked": 0.0, "value": None})
    r = client.get("/api/throughput/stats")
    body = r.json()
    assert body["active_model"] == "Qwen-Test-Q6_K.gguf"
    assert body["active_model_alias"] == "qwen-test-q6_k"
    assert body["control_plane_ok"] is True


def test_throughput_stats_active_model_null_when_ops_down(client, monkeypatch):
    """ops-controller unreachable -> active_model is null (honest unknown), the endpoint
    still serves stats, and the failure is negatively cached (one upstream call)."""
    import dashboard.app as dashboard_app
    calls = {"n": 0}

    async def _fake_ops(method, path, *a, **k):
        calls["n"] += 1
        return 503, {"detail": "down"}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    monkeypatch.setattr(dashboard_app, "_active_model_cache", {"checked": 0.0, "value": None})
    first = client.get("/api/throughput/stats").json()
    second = client.get("/api/throughput/stats").json()
    assert first["active_model"] is None
    assert second["active_model"] is None
    assert first["control_plane_ok"] is False
    assert second["control_plane_ok"] is False
    assert first["active_model_alias"] is None
    assert second["active_model_alias"] is None
    assert calls["n"] == 1, "second read within TTL must hit the cache, not ops"


def test_throughput_stats_distinguishes_unconfigured_from_unreachable(client, monkeypatch):
    """ops reachable but no model configured -> active_model null with control_plane_ok
    True — the UI must not blame the control plane for an empty config."""
    import dashboard.app as dashboard_app

    async def _fake_ops(method, path, *a, **k):
        return 200, {"active_model": "", "running": {}}

    monkeypatch.setattr("dashboard.app._ops_request", _fake_ops)
    monkeypatch.setattr(dashboard_app, "_active_model_cache", {"checked": 0.0, "value": None})
    d = client.get("/api/throughput/stats").json()
    assert d["active_model"] is None
    assert d["control_plane_ok"] is True


def test_throughput_record_accepts_alias_and_backend(client):
    """v2 payload: the gateway callback attributes samples to the REAL served GGUF and
    passes the requested alias + backend service alongside. Old senders (no alias/backend)
    must keep working — both fields optional."""
    r = client.post("/api/throughput/record", json={
        "model": "Attrib-Test-Q6_K.gguf",
        "output_tokens_per_sec": 41.0,
        "service": "hermes",
        "alias": "local-chat",
        "backend": "llamacpp",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    stats = client.get("/api/throughput/stats").json()
    assert "Attrib-Test-Q6_K.gguf" in stats["models"]
    import dashboard.app as dashboard_app
    with dashboard_app._state_lock:
        evt = next(u for u in reversed(dashboard_app._service_usage)
                   if u["model"] == "Attrib-Test-Q6_K.gguf")
    assert evt["alias"] == "local-chat"
    assert evt["backend"] == "llamacpp"


def test_throughput_samples_evict_after_max_age(client):
    """Models with no sample in _SAMPLE_MAX_AGE_SEC disappear from the store — a retired
    model must not keep stats forever (the root of the old 'stale model labeled Active' lie)."""
    import dashboard.app as dashboard_app
    client.post("/api/throughput/record", json={
        "model": "evict-me.gguf", "output_tokens_per_sec": 20.0,
    })
    with dashboard_app._state_lock:
        for s in dashboard_app._throughput_samples["evict-me.gguf"]:
            s["ts"] -= dashboard_app._SAMPLE_MAX_AGE_SEC + 60
    stats = client.get("/api/throughput/stats").json()
    assert "evict-me.gguf" not in stats["models"]


def test_throughput_store_v1_file_triggers_clean_reset(tmp_path, monkeypatch):
    """A version-less (v1) throughput.json is un-timestamped and alias-conflated —
    loading must reset samples (keeping last_benchmark), not present legacy junk
    as honest history."""
    import json as _json

    import dashboard.app as dashboard_app
    legacy = {
        "samples": {"local-chat": [40.1, 39.0], "test-model": [25.5]},
        "ttft_samples": {},
        "last_benchmark": {"ok": True, "model": "local-chat", "output_tokens_per_sec": 40.0},
        "service_usage": [],
    }
    f = tmp_path / "throughput.json"
    f.write_text(_json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    monkeypatch.setattr(dashboard_app, "_throughput_samples", {})
    monkeypatch.setattr(dashboard_app, "_ttft_samples", {})
    monkeypatch.setattr(dashboard_app, "_service_usage", [])
    monkeypatch.setattr(dashboard_app, "_last_benchmark", None)
    dashboard_app._load_throughput_state()
    assert dashboard_app._throughput_samples == {}
    assert dashboard_app._last_benchmark["model"] == "local-chat"
    persisted = _json.loads(f.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    assert persisted["samples"] == {}


def test_throughput_store_v2_roundtrip(tmp_path, monkeypatch):
    """v2 save/load round-trips timestamped samples."""
    import dashboard.app as dashboard_app
    f = tmp_path / "throughput.json"
    monkeypatch.setattr(dashboard_app, "_THROUGHPUT_FILE", f)
    sample = {"tps": 33.3, "ts": 1_700_000_000.0}
    monkeypatch.setattr(dashboard_app, "_throughput_samples", {"M.gguf": [sample]})
    dashboard_app._save_throughput_state()
    monkeypatch.setattr(dashboard_app, "_throughput_samples", {})
    dashboard_app._load_throughput_state()
    assert dashboard_app._throughput_samples == {"M.gguf": [sample]}


def test_performance_summary_sorts_by_recency(client):
    """top_models orders by last_ts desc — a retired model with a huge lifetime
    sample_count must not outrank the model serving right now."""
    import dashboard.app as dashboard_app
    for _ in range(5):
        client.post("/api/throughput/record", json={
            "model": "old-but-many.gguf", "output_tokens_per_sec": 10.0})
    with dashboard_app._state_lock:
        for s in dashboard_app._throughput_samples["old-but-many.gguf"]:
            s["ts"] -= 3600
    client.post("/api/throughput/record", json={
        "model": "fresh.gguf", "output_tokens_per_sec": 50.0})
    top = client.get("/api/performance/summary").json()["throughput"]["top_models"]
    names = [t["model"] for t in top]
    assert names.index("fresh.gguf") < names.index("old-but-many.gguf")
    assert all("last_ts" in t for t in top)


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
