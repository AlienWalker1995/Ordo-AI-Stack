"""Dashboard dependency diagnostics, slice 2: the remaining outbound helpers record failures.

_check_service and _check_via_edge (PROBE/EDGE, one status per target), the orchestration routes' raw ops-controller
clients and the ComfyUI queue probe (OPS/COMFYUI), the readiness sync probes (GATEWAY/COMFYUI),
the Qdrant collection count (QDRANT), the throughput benchmark (GATEWAY), _live_gpus (OPS) and
the caddy identity DNS lookup (EDGE). Each keeps its degrade-don't-500 behaviour; a 4xx other
than AUTH means the dependency answered, so it marks ok instead of recording."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dashboard import diagnostics


@pytest.fixture(autouse=True)
def clean_diagnostics():
    """Each test starts from an empty ring and no per-dependency history."""
    with diagnostics._ring_lock:
        diagnostics._failures.clear()
        diagnostics._dep_status.clear()
    yield


def _last_entry() -> dict:
    assert diagnostics._failures, "expected a recorded failure"
    return diagnostics._failures[-1]


def _dep(dep: str, target: str | None = None) -> dict:
    """The status row of a dependency, or of one target of a per-target one (PROBE, EDGE)."""
    return next(d for d in diagnostics.snapshot()["dependencies"] if d["dep"] == dep and d["target"] == target)


# --- _check_service: the hub liveness checks record under PROBE, per card ----------


def test_check_service_success_marks_probe_ok_and_passes_its_own_timeout():
    from dashboard.services_catalog import _check_service

    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200))
    ok, err = asyncio.run(_check_service("http://open-webui:3000/api/status", client, card_id="open-webui"))
    assert (ok, err) == (True, "")
    assert _dep("PROBE", "open-webui")["status"] == "ok"
    # The probe carries its own 3 s timeout, not the shared client's 30 s default.
    assert client.get.call_args.kwargs["timeout"] == 3.0


def test_check_service_connection_error_records_dep_probe_unreachable_under_the_card():
    from dashboard.services_catalog import _check_service

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    ok, err = asyncio.run(_check_service("http://langfuse:3000/health", client, card_id="langfuse"))
    assert ok is False and "refused" in err
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-PROBE-UNREACHABLE", "PROBE", "UNREACHABLE", "langfuse")
    row = _dep("PROBE", "langfuse")
    assert row["status"] == "failing" and row["consecutive_failures"] == 1


def test_check_service_5xx_records_dep_probe_http5xx_and_reads_down():
    from dashboard.services_catalog import _check_service

    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=503))
    ok, err = asyncio.run(_check_service("http://open-webui:3000/", client, card_id="open-webui"))
    assert ok is False and err == "HTTP 503"
    entry = _last_entry()
    assert (entry["code"], entry["kind"], entry["target"]) == ("DEP-PROBE-HTTP5XX", "HTTP5XX", "open-webui")
    assert _dep("PROBE", "open-webui")["status"] == "failing"


def test_check_service_protocol_error_reads_as_ok():
    # The peer closed the connection mid-answer: the service is up, so no failure is recorded.
    from dashboard.services_catalog import _check_service

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RemoteProtocolError("peer closed"))
    ok, err = asyncio.run(_check_service("http://couchdb:5984/_up", client, card_id="couchdb"))
    assert (ok, err) == (True, "")
    assert diagnostics._failures == [] and _dep("PROBE", "couchdb")["status"] == "ok"


def test_check_service_refused_reads_down_even_when_the_message_says_closed():
    from dashboard.services_catalog import _check_service

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused; socket closed"))
    ok, _err = asyncio.run(_check_service("http://couchdb:5984/_up", client, card_id="couchdb"))
    assert ok is False and _last_entry()["code"] == "DEP-PROBE-UNREACHABLE"


def test_one_healthy_card_does_not_reset_another_cards_failures():
    # Three down-then-up pairs: the down card keeps its own failure count while its neighbour
    # answers, instead of one shared PROBE status that reads ok with 0 failures.
    from dashboard.services_catalog import _check_service

    down = MagicMock()
    down.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    up = MagicMock()
    up.get = AsyncMock(return_value=MagicMock(status_code=200))
    for _ in range(3):
        asyncio.run(_check_service("http://langfuse:3000/", down, card_id="langfuse"))
        asyncio.run(_check_service("http://open-webui:3000/", up, card_id="open-webui"))
    assert _dep("PROBE", "langfuse")["status"] == "failing"
    assert _dep("PROBE", "langfuse")["consecutive_failures"] == 3
    assert _dep("PROBE", "open-webui")["status"] == "ok"
    assert _dep("PROBE", "open-webui")["consecutive_failures"] == 0


# --- _check_via_edge: the tailnet edge_check probes record under EDGE, per check -----


def _edge_card(card_id: str = "hermes-dashboard") -> dict:
    return {"id": card_id, "tailnet_label": "hermes", "sso_port": "8443", "edge_check": "/api/status"}


@pytest.fixture
def edge_env(monkeypatch):
    monkeypatch.setenv("CADDY_TAILNET_HOSTNAME", "caddy.tailnet.ts.net")
    monkeypatch.setenv("CADDY_TAILNET_DOMAIN", "ts.net")


def _edge_client(response=None, raises=None):
    client = MagicMock()
    client.build_request = MagicMock(return_value=object())
    client.send = AsyncMock(return_value=response, side_effect=raises)
    return client


def test_check_via_edge_5xx_records_dep_edge_http5xx_under_the_check(edge_env):
    from dashboard.services_catalog import _check_via_edge

    ok, err = asyncio.run(_check_via_edge(_edge_card(), _edge_client(MagicMock(status_code=502))))
    assert ok is False and "502" in err
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-EDGE-HTTP5XX", "EDGE", "HTTP5XX", "hermes-dashboard")
    assert _dep("EDGE", "hermes-dashboard")["status"] == "failing"


def test_check_via_edge_request_error_records_dep_edge_unreachable(edge_env):
    from dashboard.services_catalog import _check_via_edge

    ok, err = asyncio.run(_check_via_edge(_edge_card(), _edge_client(raises=httpx.ConnectError("refused"))))
    assert ok is False and "refused" in err
    assert _last_entry()["code"] == "DEP-EDGE-UNREACHABLE"


def test_check_via_edge_answer_marks_edge_ok(edge_env):
    from dashboard.services_catalog import _check_via_edge

    ok, err = asyncio.run(_check_via_edge(_edge_card(), _edge_client(MagicMock(status_code=200))))
    assert (ok, err) == (True, "")
    assert diagnostics._failures == [] and _dep("EDGE", "hermes-dashboard")["status"] == "ok"


def test_a_healthy_edge_check_or_dns_lookup_does_not_reset_another_edge_check(edge_env):
    from dashboard import auth
    from dashboard.services_catalog import _check_via_edge

    broken = _edge_client(MagicMock(status_code=502))
    healthy = _edge_client(MagicMock(status_code=200))
    infos = [(2, 1, 6, "", ("10.0.0.5", 0))]
    for _ in range(3):
        asyncio.run(_check_via_edge(_edge_card("hermes-dashboard"), broken))
        asyncio.run(_check_via_edge(_edge_card("langfuse"), healthy))
        with patch.object(auth.socket, "getaddrinfo", return_value=infos):
            auth.edge_proxy_addresses()
    assert _dep("EDGE", "hermes-dashboard")["status"] == "failing"
    assert _dep("EDGE", "hermes-dashboard")["consecutive_failures"] == 3
    assert _dep("EDGE", "langfuse")["status"] == "ok"
    assert _dep("EDGE", "dns:caddy")["status"] == "ok"


def test_diagnostics_lists_each_probe_and_edge_target_as_its_own_row(edge_env):
    import dashboard.app as app
    from dashboard.services_catalog import _check_service, _check_via_edge

    down = MagicMock()
    down.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    up = MagicMock()
    up.get = AsyncMock(return_value=MagicMock(status_code=200))
    asyncio.run(_check_service("http://langfuse:3000/", down, card_id="langfuse"))
    asyncio.run(_check_service("http://open-webui:3000/", up, card_id="open-webui"))
    asyncio.run(_check_via_edge(_edge_card(), _edge_client(MagicMock(status_code=502))))

    rows = TestClient(app.app).get("/api/diagnostics").json()["dependencies"]
    by_key = {(row["dep"], row["target"]): row for row in rows}
    assert by_key[("PROBE", "langfuse")]["status"] == "failing"
    assert by_key[("PROBE", "open-webui")]["status"] == "ok"
    assert by_key[("EDGE", "hermes-dashboard")]["status"] == "failing"
    # No bare PROBE/EDGE row once targets exist; a dependency that is one target keeps a null one.
    assert ("PROBE", None) not in by_key and ("EDGE", None) not in by_key
    assert by_key[("OPS", None)]["status"] == "unknown"
    # Targets sort within their dependency.
    probe_targets = [row["target"] for row in rows if row["dep"] == "PROBE"]
    assert probe_targets == sorted(probe_targets)


def test_a_per_target_dependency_refuses_a_status_without_a_target():
    with pytest.raises(ValueError):
        diagnostics.mark_ok("PROBE")


# --- a recorded failure that becomes an HTTP error carries its DEP code -------------


def test_ops_proxy_error_body_carries_the_dep_code(monkeypatch, dashboard_operator_headers):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    client = MagicMock()
    client.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("dashboard.app._get_http_client", return_value=client):
        r = TestClient(app.app, headers=dashboard_operator_headers).post(
            "/api/ops/services/open-webui/restart", json={"confirm": True})
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "DEP-OPS-UNREACHABLE"
    assert body["detail"] == "ConnectError: refused"
    assert r.headers["X-Trace-Id"] == diagnostics._failures[-1]["trace"]


def test_ops_proxy_answered_4xx_has_no_dep_code(monkeypatch, dashboard_operator_headers):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    answer = MagicMock(status_code=409)
    answer.json.return_value = {"error": "busy"}
    client = MagicMock()
    client.request = AsyncMock(return_value=answer)
    with patch("dashboard.app._get_http_client", return_value=client):
        r = TestClient(app.app, headers=dashboard_operator_headers).post(
            "/api/ops/services/open-webui/restart", json={"confirm": True})
    assert r.status_code == 409 and r.json() == {"detail": "busy"}


def test_orchestration_502_body_carries_the_dep_code(monkeypatch, dashboard_operator_headers):
    import dashboard.app as app
    from dashboard import routes_orchestration

    monkeypatch.setattr(routes_orchestration, "OPS_CONTROLLER_TOKEN", "t")
    factory, _ = _raw_client(raises=httpx.ReadTimeout("read"))
    with patch.object(routes_orchestration.httpx, "AsyncClient", factory):
        r = TestClient(app.app, headers=dashboard_operator_headers).get("/api/orchestration/registry/models")
    assert r.status_code == 502
    assert r.json() == {"detail": "read", "code": "DEP-OPS-TIMEOUT"}


def test_benchmark_502_body_carries_the_dep_code(monkeypatch, dashboard_operator_headers):
    import dashboard.app as app

    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=503))
    with patch("dashboard.app._get_http_client", return_value=client):
        r = TestClient(app.app, headers=dashboard_operator_headers).post(
            "/api/throughput/benchmark", json={"model": "local-chat"})
    assert r.status_code == 502
    assert r.json() == {"detail": "Model gateway answered HTTP 503", "code": "DEP-GATEWAY-HTTP5XX"}


# --- the orchestration routes' raw clients record under OPS / COMFYUI --------------


def _raw_client(response=None, raises=None):
    """A stand-in for the httpx.AsyncClient these routes open per call."""
    instance = MagicMock()
    if raises is not None:
        instance.request = AsyncMock(side_effect=raises)
        instance.get = AsyncMock(side_effect=raises)
    else:
        instance.request = AsyncMock(return_value=response)
        instance.get = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=instance)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), instance


def test_raw_ops_request_5xx_records_dep_ops_http5xx():
    from dashboard import routes_orchestration

    factory, _ = _raw_client(response=MagicMock(status_code=502, text="boom"))
    with patch.object(routes_orchestration.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(routes_orchestration._raw_ops_request(
                "POST", "/services/comfyui/restart", None, timeout=60.0, headers={}))
    assert excinfo.value.status_code == 502
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-OPS-HTTP5XX", "OPS", "HTTP5XX", "/services/comfyui/restart")


def test_raw_ops_request_timeout_records_dep_ops_timeout():
    from dashboard import routes_orchestration

    factory, _ = _raw_client(raises=httpx.ReadTimeout("read"))
    with patch.object(routes_orchestration.httpx, "AsyncClient", factory):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(routes_orchestration._raw_ops_request(
                "GET", "/registry/models", None, timeout=15.0, headers={}))
    assert excinfo.value.status_code == 502
    assert _last_entry()["code"] == "DEP-OPS-TIMEOUT"


def test_raw_ops_request_success_marks_ops_ok():
    from dashboard import routes_orchestration

    response = MagicMock(status_code=200)
    response.json.return_value = {"models": {}}
    factory, _ = _raw_client(response=response)
    with patch.object(routes_orchestration.httpx, "AsyncClient", factory):
        out = asyncio.run(routes_orchestration._raw_ops_request(
            "GET", "/registry/gpus", None, timeout=15.0, headers={}))
    assert out == {"models": {}}
    assert diagnostics._failures == [] and _dep("OPS")["status"] == "ok"


def test_comfyui_status_queue_timeout_records_dep_comfyui_timeout(monkeypatch):
    from dashboard import routes_orchestration

    monkeypatch.setattr(routes_orchestration, "OPS_CONTROLLER_TOKEN", "t")
    monkeypatch.setattr(routes_orchestration, "COMFYUI_URL", "http://comfyui-gate:8188")
    services = MagicMock(status_code=200)
    services.json.return_value = {"services": [{"id": "comfyui", "state": "running"}]}
    factory, instance = _raw_client(response=services)
    instance.get = AsyncMock(side_effect=[services, httpx.ReadTimeout("read")])
    with patch.object(routes_orchestration.httpx, "AsyncClient", factory):
        out = asyncio.run(routes_orchestration.comfyui_status(None))
    assert out["container_state"] == "running" and out["queue"]["reachable"] is False
    assert [e["code"] for e in diagnostics._failures] == ["DEP-COMFYUI-TIMEOUT"]
    assert _dep("OPS")["status"] == "ok" and _dep("COMFYUI")["status"] == "failing"


# --- the readiness sync probes record under their dep ------------------------------


def _sync_client(response=None, raises=None):
    """A stand-in for the httpx.Client the readiness probes open per call."""
    instance = MagicMock()
    if raises is not None:
        instance.get = MagicMock(side_effect=raises)
        instance.post = MagicMock(side_effect=raises)
    else:
        instance.get = MagicMock(return_value=response)
        instance.post = MagicMock(return_value=response)
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=instance)
    cm.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=cm), instance


def test_probe_get_5xx_records_under_its_dep():
    from dashboard import orchestration_readiness

    factory, _ = _sync_client(response=MagicMock(status_code=500))
    with patch.object(orchestration_readiness.httpx, "Client", factory):
        ok, err = orchestration_readiness._probe_get("http://model-gateway:4000/ready", dep="GATEWAY")
    assert ok is False and err == "HTTP 500"
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-GATEWAY-HTTP5XX", "GATEWAY", "HTTP5XX", "/ready")


def test_probe_get_media_probe_records_under_comfyui():
    # The readiness media probe passes dep="COMFYUI"; the code must ride along.
    from dashboard import orchestration_readiness

    factory, _ = _sync_client(raises=httpx.ReadTimeout("read"))
    with patch.object(orchestration_readiness.httpx, "Client", factory):
        ok, err = orchestration_readiness._probe_get("http://comfyui-gate:8188/", dep="COMFYUI")
    assert ok is False
    assert _last_entry()["code"] == "DEP-COMFYUI-TIMEOUT"


def test_probe_get_answer_marks_the_dep_ok():
    from dashboard import orchestration_readiness

    factory, _ = _sync_client(response=MagicMock(status_code=200))
    with patch.object(orchestration_readiness.httpx, "Client", factory):
        ok, err = orchestration_readiness._probe_get("http://model-gateway:4000/ready", dep="GATEWAY")
    assert (ok, err) == (True, None)
    assert diagnostics._failures == [] and _dep("GATEWAY")["status"] == "ok"


def test_probe_litellm_mcp_tools_failure_records_gateway():
    from dashboard import orchestration_readiness

    factory, _ = _sync_client(raises=httpx.ConnectError("refused"))
    with patch.object(orchestration_readiness.httpx, "Client", factory):
        ok, tools, err = orchestration_readiness._probe_litellm_mcp_tools(
            "http://model-gateway:4000/mcp", "key")
    assert ok is False and tools == 0 and "refused" in err
    assert _last_entry()["code"] == "DEP-GATEWAY-UNREACHABLE"


# --- the Qdrant collection count records under QDRANT -------------------------------


def test_rag_status_5xx_records_dep_qdrant_http5xx():
    import dashboard.app as app

    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=500))
    with patch("dashboard.app._get_http_client", return_value=client):
        out = asyncio.run(app.rag_status())
    assert out["ok"] is False
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-QDRANT-HTTP5XX", "QDRANT", "HTTP5XX", "/collections/documents")


def test_rag_status_unreachable_records_dep_qdrant_unreachable():
    import dashboard.app as app

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("dashboard.app._get_http_client", return_value=client):
        out = asyncio.run(app.rag_status())
    assert out["ok"] is False
    assert _last_entry()["code"] == "DEP-QDRANT-UNREACHABLE"


def test_rag_status_ok_marks_qdrant_ok():
    import dashboard.app as app

    response = MagicMock(status_code=200)
    response.json.return_value = {"result": {"points_count": 5, "status": "green"}}
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    with patch("dashboard.app._get_http_client", return_value=client):
        out = asyncio.run(app.rag_status())
    assert out["ok"] is True and out["points_count"] == 5
    assert diagnostics._failures == [] and _dep("QDRANT")["status"] == "ok"


# --- the throughput benchmark records under GATEWAY ---------------------------------


def test_throughput_benchmark_5xx_records_dep_gateway_http5xx_once():
    import dashboard.app as app

    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=500))
    with patch("dashboard.app._get_http_client", return_value=client):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(app.throughput_benchmark(app.ThroughputBenchmarkRequest(model="local-chat")))
    assert excinfo.value.status_code == 502
    # Exactly one record: the explicit 5xx branch must not fall through to a second one.
    assert len(diagnostics._failures) == 1
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-GATEWAY-HTTP5XX", "GATEWAY", "HTTP5XX", "/v1/chat/completions")


def test_throughput_benchmark_connection_error_records_dep_gateway_unreachable():
    import dashboard.app as app

    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("dashboard.app._get_http_client", return_value=client):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(app.throughput_benchmark(app.ThroughputBenchmarkRequest(model="local-chat")))
    assert excinfo.value.status_code == 502
    assert _last_entry()["code"] == "DEP-GATEWAY-UNREACHABLE"


# --- _live_gpus records a 200 without an object body under OPS ----------------------


def test_live_gpus_non_object_body_records_dep_ops_badpayload():
    import dashboard.app as app

    with patch("dashboard.app._ops_request", new=AsyncMock(return_value=(200, "garbage"))):
        gpus = asyncio.run(app._live_gpus())
    assert gpus == []
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-OPS-BADPAYLOAD", "OPS", "BADPAYLOAD", "/gpus")


# --- the caddy identity DNS lookup records under EDGE --------------------------------


def test_edge_proxy_addresses_dns_error_records_dep_edge_unreachable():
    from dashboard import auth

    with patch.object(auth.socket, "getaddrinfo", side_effect=OSError("no address")):
        addrs = auth.edge_proxy_addresses()
    assert addrs == set()
    entry = _last_entry()
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-EDGE-UNREACHABLE", "EDGE", "UNREACHABLE", "dns:caddy")
    assert _dep("EDGE", "dns:caddy")["status"] == "failing"


def test_edge_proxy_addresses_success_marks_edge_ok():
    from dashboard import auth

    infos = [(2, 1, 6, "", ("10.0.0.5", 0))]
    with patch.object(auth.socket, "getaddrinfo", return_value=infos):
        addrs = auth.edge_proxy_addresses()
    assert addrs == {"10.0.0.5"}
    assert diagnostics._failures == [] and _dep("EDGE", "dns:caddy")["status"] == "ok"
