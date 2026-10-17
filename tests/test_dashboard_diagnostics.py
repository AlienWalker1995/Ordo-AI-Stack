"""Dashboard dependency diagnostics: DEP-<DEP>-<KIND> codes, trace ids, the failure ring,
_ops_request/_get_json recording, X-Trace-Id on every response, and GET /api/diagnostics."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean_diagnostics():
    """Each test starts from an empty ring and no per-dependency history."""
    from dashboard import diagnostics

    with diagnostics._ring_lock:
        diagnostics._failures.clear()
        diagnostics._dep_status.clear()
    yield


# --- classify_failure: the exception and status mapping -------------------------


@pytest.mark.parametrize("exc, kind", [
    (httpx.ConnectError("refused"), "UNREACHABLE"),
    (httpx.ReadError("broken pipe"), "UNREACHABLE"),
    (httpx.RemoteProtocolError("peer closed"), "UNREACHABLE"),
    (OSError("dns failed"), "UNREACHABLE"),
    (httpx.ConnectTimeout("connect"), "TIMEOUT"),
    (httpx.ReadTimeout("read"), "TIMEOUT"),
    (httpx.WriteTimeout("write"), "TIMEOUT"),
    (httpx.PoolTimeout("pool"), "TIMEOUT"),
])
def test_classify_failure_maps_exceptions(exc, kind):
    from dashboard import diagnostics

    assert diagnostics.classify_failure(exc) == kind


@pytest.mark.parametrize("status, kind", [
    (500, "HTTP5XX"), (502, "HTTP5XX"), (503, "HTTP5XX"),
    (400, "HTTP4XX"), (404, "HTTP4XX"), (429, "HTTP4XX"),
    (401, "AUTH"), (403, "AUTH"),
])
def test_classify_failure_maps_statuses(status, kind):
    from dashboard import diagnostics

    assert diagnostics.classify_failure(status=status) == kind


def test_classify_failure_refuses_a_success_status():
    from dashboard import diagnostics

    with pytest.raises(ValueError):
        diagnostics.classify_failure(status=200)


# --- record_failure: one WARNING line, code and trace, no tokens -----------------


def test_record_failure_logs_one_warning_with_code_and_trace(caplog):
    from dashboard import diagnostics

    token = diagnostics.set_trace_id("abc123def456")
    try:
        with caplog.at_level(logging.WARNING, logger="dashboard.diagnostics"):
            code = diagnostics.record_failure(
                "OPS", "TIMEOUT", "ops-controller did not answer GET /model-config within 3 s",
                target="/model-config", elapsed_ms=3000.4,
            )
    finally:
        diagnostics.reset_trace_id(token)
    assert code == "DEP-OPS-TIMEOUT"
    records = [r for r in caplog.records if r.name == "dashboard.diagnostics"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "DEP_FAIL code=DEP-OPS-TIMEOUT trace=abc123def456 target=/model-config" in message
    assert "elapsed_ms=3000" in message
    assert "did not answer GET /model-config within 3 s" in message
    # The detail carries no credentials: nothing a caller would put in a header reaches the log.
    assert "Bearer" not in message and "Authorization" not in message


def test_record_failure_truncates_the_detail_to_200_chars():
    from dashboard import diagnostics

    long_detail = "x" * 300
    diagnostics.record_failure("FS", "UNREACHABLE", long_detail, target="/data/dashboard", elapsed_ms=1.0)
    entry = diagnostics._failures[-1]
    assert len(entry["detail"]) == 200 and entry["detail"] == "x" * 200


def test_record_failure_strips_urls_from_the_detail_before_it_enters_the_ring():
    # str(exc) of an httpx error embeds the full request URL; the ring is open-read, so no
    # internal hostname, port or path may survive into it.
    from dashboard import diagnostics

    diagnostics.record_failure(
        "OPS", "UNREACHABLE",
        "ConnectError: Connection refused (GET http://ops-controller:9000/stats/services)",
        target="/stats/services", elapsed_ms=1.0,
    )
    entry = diagnostics._failures[-1]
    assert "://" not in entry["detail"]
    assert entry["detail"] == "ConnectError: Connection refused (GET <url>)"


def test_ring_caps_at_200_newest_first_in_the_snapshot():
    from dashboard import diagnostics

    for i in range(205):
        diagnostics.record_failure("PROBE", "UNREACHABLE", f"probe {i}", target="/probe", elapsed_ms=1.0)
    assert len(diagnostics._failures) == 200
    assert diagnostics._failures[0]["detail"] == "probe 5"      # the oldest five were evicted
    recent = diagnostics.snapshot()["recent"]
    assert recent[0]["detail"] == "probe 204"                   # newest first


def test_mark_ok_resets_consecutive_failures():
    from dashboard import diagnostics

    diagnostics.record_failure("QDRANT", "UNREACHABLE", "refused", target="/collections/documents", elapsed_ms=1.0)
    diagnostics.record_failure("QDRANT", "UNREACHABLE", "refused", target="/collections/documents", elapsed_ms=1.0)
    st = diagnostics._dep_status["QDRANT"]
    assert st["consecutive_failures"] == 2
    diagnostics.mark_ok("QDRANT")
    assert st["consecutive_failures"] == 0 and st["last_ok"] is not None


# --- _ops_request: records DEP-OPS-* and still degrades to 503 --------------------


def _ops_client(response=None, raises=None):
    c = MagicMock()
    if raises is not None:
        c.request = AsyncMock(side_effect=raises)
    else:
        c.request = AsyncMock(return_value=response)
    return c


def test_ops_request_timeout_records_dep_ops_timeout_and_returns_503(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _ops_client(raises=httpx.ReadTimeout("read"))
    with patch("dashboard.app._get_http_client", return_value=ops):
        code, data = asyncio.run(app._ops_request("GET", "/model-config", timeout=3.0))
    assert code == 503
    assert data["detail"] == "ops-controller did not answer GET /model-config within 3 s"
    from dashboard import diagnostics
    entry = diagnostics._failures[-1]
    assert (entry["code"], entry["dep"], entry["kind"], entry["target"]) == \
        ("DEP-OPS-TIMEOUT", "OPS", "TIMEOUT", "/model-config")
    # The trace id rides along as X-Request-ID for ops-controller's audit log.
    token = diagnostics.set_trace_id("audit-trace-1")
    try:
        with patch("dashboard.app._get_http_client", return_value=ops):
            asyncio.run(app._ops_request("GET", "/model-config", timeout=3.0))
    finally:
        diagnostics.reset_trace_id(token)
    assert ops.request.call_args_list[-1].kwargs["headers"]["X-Request-ID"] == "audit-trace-1"


def test_ops_request_connection_error_records_unreachable(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    with patch("dashboard.app._get_http_client", return_value=_ops_client(raises=httpx.ConnectError("refused"))):
        code, data = asyncio.run(app._ops_request("GET", "/status", timeout=15.0))
    assert code == 503 and data["detail"] == "refused"
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-OPS-UNREACHABLE"


def test_ops_request_missing_token_records_not_configured(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "")
    code, data = asyncio.run(app._ops_request("GET", "/status", timeout=15.0))
    assert code == 503 and data["detail"] == "OPS_CONTROLLER_TOKEN not configured"
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-OPS-NOTCONFIGURED"


def test_ops_request_non_200_records_the_status_kind_and_success_marks_ok(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    bad = MagicMock(status_code=502)
    bad.json.return_value = {"error": "render failed"}
    with patch("dashboard.app._get_http_client", return_value=_ops_client(response=bad)):
        code, data = asyncio.run(app._ops_request("POST", "/model-config", timeout=5.0))
    assert code == 502 and data == {"error": "render failed"}
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-OPS-HTTP5XX"

    good = MagicMock(status_code=200)
    good.json.return_value = {"ok": True}
    with patch("dashboard.app._get_http_client", return_value=_ops_client(response=good)):
        code, _ = asyncio.run(app._ops_request("GET", "/status", timeout=15.0))
    assert code == 200
    st = diagnostics._dep_status["OPS"]
    assert st["consecutive_failures"] == 0 and st["last_ok"] is not None


def test_ops_request_marks_ok_on_a_non_auth_4xx(monkeypatch):
    # A model switch ops-controller rejects with 409 (substrate digest mismatch) or a 404: the
    # controller answered, so OPS stays ok instead of climbing consecutive_failures.
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    conflict = MagicMock(status_code=409)
    conflict.json.return_value = {"error": "substrate digest mismatch"}
    with patch("dashboard.app._get_http_client", return_value=_ops_client(response=conflict)):
        code, data = asyncio.run(app._ops_request("POST", "/model-config", timeout=5.0))
    assert code == 409 and data == {"error": "substrate digest mismatch"}
    from dashboard import diagnostics
    assert all(f["dep"] != "OPS" for f in diagnostics._failures)
    st = diagnostics._dep_status["OPS"]
    assert st["last_ok"] is not None and st["consecutive_failures"] == 0


def test_ops_request_records_auth_on_401(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    unauthorized = MagicMock(status_code=401)
    unauthorized.json.return_value = {"error": "bad token"}
    with patch("dashboard.app._get_http_client", return_value=_ops_client(response=unauthorized)):
        code, _ = asyncio.run(app._ops_request("GET", "/status", timeout=15.0))
    assert code == 401
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-OPS-AUTH"


def test_ops_request_records_badpayload_on_an_unparseable_body(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    r = MagicMock(status_code=200)
    r.json.side_effect = ValueError("not json")
    r.text = "<html>bad gateway</html>"
    with patch("dashboard.app._get_http_client", return_value=_ops_client(response=r)):
        code, data = asyncio.run(app._ops_request("GET", "/status", timeout=15.0))
    assert code == 200 and data == {"detail": "<html>bad gateway</html>"}
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-OPS-BADPAYLOAD"


def test_ops_request_caller_supplied_x_request_id_wins_over_the_trace_id(monkeypatch):
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    good = MagicMock(status_code=200)
    good.json.return_value = {"ok": True}
    ops = _ops_client(response=good)
    with patch("dashboard.app._get_http_client", return_value=ops):
        asyncio.run(app._ops_request("GET", "/status", timeout=15.0,
                                     headers={"X-Request-ID": "caller-override"}))
    assert ops.request.call_args.kwargs["headers"]["X-Request-ID"] == "caller-override"


def test_ops_request_has_no_dead_request_parameter():
    # The outbound X-Request-ID comes from the trace id (current_trace_id), not the raw inbound
    # header, so _ops_request never needed the Request object; pin the signature so the dead
    # parameter does not come back.
    import inspect

    import dashboard.app as app

    assert "request" not in inspect.signature(app._ops_request).parameters


# --- _get_json: non-200 is no longer silent ---------------------------------------


def _get_json(dep, url, response=None, raises=None):
    from dashboard import routes_console

    client = MagicMock()
    if raises is not None:
        client.get = AsyncMock(side_effect=raises)
    else:
        client.get = AsyncMock(return_value=response)
    with patch("dashboard.app._get_http_client", return_value=client):
        return asyncio.run(routes_console._get_json(url, 5.0, dep=dep))


def test_get_json_records_http5xx_on_a_server_error():
    r = MagicMock(status_code=500)
    r.json.return_value = {}
    assert _get_json("PROMETHEUS", "http://prometheus:9090/api/v1/query_range", response=r) is None
    from dashboard import diagnostics
    entry = diagnostics._failures[-1]
    assert (entry["code"], entry["target"]) == ("DEP-PROMETHEUS-HTTP5XX", "/api/v1/query_range")


def test_get_json_marks_ok_on_a_non_auth_4xx():
    # A 4xx other than AUTH means the dependency answered — it is up, so no failure is recorded.
    r = MagicMock(status_code=404)
    r.json.return_value = {}
    assert _get_json("GRAFANA", "http://grafana:3000/api/health", response=r) is None
    from dashboard import diagnostics
    assert all(f["dep"] != "GRAFANA" for f in diagnostics._failures)
    st = diagnostics._dep_status["GRAFANA"]
    assert st["last_ok"] is not None and st["consecutive_failures"] == 0


def test_get_json_records_auth_on_401_and_403():
    for status in (401, 403):
        r = MagicMock(status_code=status)
        r.json.return_value = {}
        assert _get_json("GRAFANA", "http://grafana:3000/api/health", response=r) is None
        from dashboard import diagnostics
        assert diagnostics._failures[-1]["code"] == "DEP-GRAFANA-AUTH"


def test_get_json_records_unreachable_on_a_connection_error():
    assert _get_json("LLAMACPP", "http://llamacpp:8080/v1/models", raises=httpx.ConnectError("refused")) is None
    from dashboard import diagnostics
    entry = diagnostics._failures[-1]
    assert (entry["code"], entry["target"]) == ("DEP-LLAMACPP-UNREACHABLE", "/v1/models")


def test_get_json_records_badpayload_on_an_unparseable_body():
    r = MagicMock(status_code=200)
    r.json.side_effect = ValueError("not json")
    assert _get_json("COMFYUI", "http://comfyui-gate:8188/history", response=r) is None
    from dashboard import diagnostics
    assert diagnostics._failures[-1]["code"] == "DEP-COMFYUI-BADPAYLOAD"


def test_get_json_returns_the_payload_on_success():
    r = MagicMock(status_code=200)
    r.json.return_value = {"database": "ok"}
    assert _get_json("GRAFANA", "http://grafana:3000/api/health", response=r) == {"database": "ok"}
    from dashboard import diagnostics
    assert diagnostics._dep_status["GRAFANA"]["last_ok"] is not None


# --- X-Trace-Id on 200, 401 and 500 responses -------------------------------------


def test_trace_header_is_present_on_a_200_response():
    import dashboard.app as app

    r = TestClient(app.app).get("/api/diagnostics")
    assert r.status_code == 200
    assert len(r.headers["X-Trace-Id"]) == 16


def test_a_sane_inbound_x_request_id_is_reused_as_the_trace_id():
    import dashboard.app as app

    r = TestClient(app.app).get("/api/diagnostics", headers={"X-Request-ID": "operator-req-1"})
    assert r.headers["X-Trace-Id"] == "operator-req-1"


def test_an_unsane_inbound_x_request_id_gets_a_fresh_trace_id():
    import dashboard.app as app

    r = TestClient(app.app).get("/api/diagnostics", headers={"X-Request-ID": "a b c\n"})
    assert r.headers["X-Trace-Id"] != "a b c\n" and len(r.headers["X-Trace-Id"]) == 16


def test_trace_header_is_present_on_a_401_response():
    import dashboard.app as app

    r = TestClient(app.app).post("/api/ops/services/open-webui/start", json={"confirm": True})
    assert r.status_code == 401
    assert len(r.headers["X-Trace-Id"]) == 16


def test_trace_id_is_in_the_500_body_and_header(monkeypatch):
    import dashboard.app as app

    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app.psutil, "cpu_count", _boom)
    # raise_server_exceptions=False: ServerErrorMiddleware sends the 500 and then re-raises for
    # the server to log; the client wants the response, not the traceback.
    r = TestClient(app.app, raise_server_exceptions=False).get("/api/hardware/service-pressure")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "Internal server error"
    assert body["trace_id"] == r.headers["X-Trace-Id"] and len(body["trace_id"]) == 16


# --- GET /api/diagnostics ----------------------------------------------------------


def test_diagnostics_endpoint_shape(monkeypatch):
    import dashboard.app as app
    from dashboard import diagnostics

    # One real failure so the payload has something to show.
    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _ops_client(raises=httpx.ReadTimeout("read"))
    with patch("dashboard.app._get_http_client", return_value=ops):
        asyncio.run(app._ops_request("GET", "/status", timeout=15.0))

    r = TestClient(app.app).get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"generated_at", "dependencies", "recent", "codes"}
    deps = {d["dep"]: d for d in body["dependencies"]}
    assert set(deps) == set(diagnostics.DEPENDENCIES)
    for d in body["dependencies"]:
        assert set(d) == {"dep", "status", "last_ok", "last_failure", "consecutive_failures"}
        assert d["status"] in ("ok", "failing", "unknown")
    ops_dep = deps["OPS"]
    assert ops_dep["status"] == "failing"
    assert ops_dep["consecutive_failures"] == 1
    assert ops_dep["last_failure"]["code"] == "DEP-OPS-TIMEOUT"
    assert body["recent"][0]["code"] == "DEP-OPS-TIMEOUT"     # newest first
    codes = {c["code"] for c in body["codes"]}
    assert {"DEP-OPS-TIMEOUT", "DEP-LLAMACPP-UNREACHABLE", "DEP-QDRANT-AUTH"} <= codes
    assert all(set(c) == {"code", "meaning"} for c in body["codes"])


def test_diagnostics_codes_do_not_advertise_http4xx():
    # A non-AUTH 4xx marks the dependency ok (it answered), so no DEP-*-HTTP4XX code is ever
    # recorded; the codes table must not advertise one an operator could never find in the ring.
    import dashboard.app as app

    body = TestClient(app.app).get("/api/diagnostics").json()
    assert all(not c["code"].endswith("-HTTP4XX") for c in body["codes"])


def test_diagnostics_reports_unknown_until_anything_is_observed():
    import dashboard.app as app

    body = TestClient(app.app).get("/api/diagnostics").json()
    deps = {d["dep"]: d for d in body["dependencies"]}
    assert all(d["status"] == "unknown" and d["last_ok"] is None and d["last_failure"] is None
               and d["consecutive_failures"] == 0 for d in body["dependencies"])
    assert body["recent"] == []
    assert deps["GATEWAY"]["dep"] == "GATEWAY"


def test_diagnostics_endpoint_details_are_sanitized_for_open_viewers(monkeypatch):
    # The open view must not leak internal hostnames or ports from raw exception text: the ring
    # carries the exception class plus a URL-stripped summary.
    import dashboard.app as app

    monkeypatch.setattr(app, "OPS_CONTROLLER_TOKEN", "t")
    ops = _ops_client(raises=OSError("connect to http://ops-controller:9000 refused"))
    with patch("dashboard.app._get_http_client", return_value=ops):
        asyncio.run(app._ops_request("GET", "/status", timeout=15.0))

    body = TestClient(app.app).get("/api/diagnostics").json()
    detail = body["recent"][0]["detail"]
    assert detail.startswith("OSError:")
    assert "ops-controller:9000" not in detail and "://" not in detail
