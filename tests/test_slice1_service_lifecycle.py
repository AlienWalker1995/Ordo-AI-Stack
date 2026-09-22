"""Slice 1: Service lifecycle routes ported from ops-api to ops-controller.

Proves each ported route on ops-controller produces the same behavior as the
original ops-api route it replaces.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from ordo.broker import MockBackend, Broker
from ordo.scheduler import Scheduler
from ordo.control import ControlPlane


@pytest.fixture
def mock_backend():
    return MockBackend()


@pytest.fixture
def scheduler():
    return Scheduler(total_vram_gb=24.0)


@pytest.fixture
def broker(scheduler, mock_backend):
    return Broker(scheduler, mock_backend)


@pytest.fixture
def control_plane(tmp_path, broker):
    """ControlPlane with broker for service lifecycle routes."""
    cp = ControlPlane(
        source_path=tmp_path / "ordo.yaml",
        catalog=MagicMock(),
        registry=MagicMock(),
        out_dir=tmp_path,
        scheduler=broker.scheduler,
        broker=broker,
    )
    return cp


class TestServiceStart:
    """POST /services/{id}/start — ported from ops-api."""

    def test_dry_run_returns_would_start(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/start", {"dry_run": True})
        assert status == 200
        assert body["would"] == "start"
        assert body["service"] == "test-svc"

    def test_requires_confirm(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/start", {})
        assert status == 400
        assert "confirm" in body["error"]

    def test_starts_service(self, control_plane, mock_backend):
        status, body = control_plane.route("POST", "/services/test-svc/start", {"confirm": True})
        assert status == 200
        assert body["ok"] is True
        assert body["service"] == "test-svc"
        assert body["action"] == "started"
        assert "test-svc" in mock_backend.started

    def test_error_on_start_failure(self, control_plane, mock_backend):
        mock_backend.start = MagicMock(side_effect=Exception("docker failed"))
        status, body = control_plane.route("POST", "/services/test-svc/start", {"confirm": True})
        assert status == 500
        assert "docker failed" in body["error"]


class TestServiceStop:
    """POST /services/{id}/stop — ported from ops-api."""

    def test_dry_run_returns_would_stop(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/stop", {"dry_run": True})
        assert status == 200
        assert body["would"] == "stop"
        assert body["service"] == "test-svc"

    def test_requires_confirm(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/stop", {})
        assert status == 400
        assert "confirm" in body["error"]

    def test_stops_service(self, control_plane, mock_backend):
        status, body = control_plane.route("POST", "/services/test-svc/stop", {"confirm": True})
        assert status == 200
        assert body["ok"] is True
        assert body["service"] == "test-svc"
        assert body["action"] == "stopped"
        assert "test-svc" in mock_backend.stopped

    def test_error_on_stop_failure(self, control_plane, mock_backend):
        mock_backend.stop = MagicMock(side_effect=Exception("docker failed"))
        status, body = control_plane.route("POST", "/services/test-svc/stop", {"confirm": True})
        assert status == 500
        assert "docker failed" in body["error"]


class TestServiceRestart:
    """POST /services/{id}/restart — ported from ops-api."""

    def test_dry_run_returns_would_restart(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/restart", {"dry_run": True})
        assert status == 200
        assert body["would"] == "restart"
        assert body["service"] == "test-svc"

    def test_requires_confirm(self, control_plane):
        status, body = control_plane.route("POST", "/services/test-svc/restart", {})
        assert status == 400
        assert "confirm" in body["error"]

    def test_restarts_service(self, control_plane, mock_backend):
        status, body = control_plane.route("POST", "/services/test-svc/restart", {"confirm": True})
        assert status == 200
        assert body["ok"] is True
        assert body["service"] == "test-svc"
        assert body["action"] == "restarted"
        assert "test-svc" in mock_backend.restarted

    def test_error_on_restart_failure(self, control_plane, mock_backend):
        mock_backend.restart = MagicMock(side_effect=Exception("docker failed"))
        status, body = control_plane.route("POST", "/services/test-svc/restart", {"confirm": True})
        assert status == 500
        assert "docker failed" in body["error"]


class TestServiceLogs:
    """GET /services/{id}/logs — ported from ops-api."""

    def test_returns_logs(self, control_plane, mock_backend):
        status, body = control_plane.route("GET", "/services/test-svc/logs")
        assert status == 200
        assert body["service"] == "test-svc"
        assert "logs" in body
        assert ("test-svc", 100) in mock_backend.log_requests

    def test_error_on_logs_failure(self, control_plane, mock_backend):
        mock_backend.logs = MagicMock(side_effect=Exception("docker failed"))
        status, body = control_plane.route("GET", "/services/test-svc/logs")
        assert status == 500
        assert "docker failed" in body["error"]


class TestRouteParityWithOpsApi:
    """Prove ops-controller routes match ops-api behavior for the same requests."""

    def test_start_response_shape_matches_ops_api(self, control_plane):
        """ops-api returns {"ok": true, "service": id, "action": "started"}."""
        status, body = control_plane.route("POST", "/services/my-svc/start", {"confirm": True})
        assert status == 200
        assert set(body.keys()) == {"ok", "service", "action"}
        assert body["ok"] is True
        assert body["service"] == "my-svc"
        assert body["action"] == "started"

    def test_stop_response_shape_matches_ops_api(self, control_plane):
        """ops-api returns {"ok": true, "service": id, "action": "stopped"}."""
        status, body = control_plane.route("POST", "/services/my-svc/stop", {"confirm": True})
        assert status == 200
        assert set(body.keys()) == {"ok", "service", "action"}
        assert body["ok"] is True
        assert body["service"] == "my-svc"
        assert body["action"] == "stopped"

    def test_restart_response_shape_matches_ops_api(self, control_plane):
        """ops-api returns {"ok": true, "service": id, "action": "restarted"}."""
        status, body = control_plane.route("POST", "/services/my-svc/restart", {"confirm": True})
        assert status == 200
        assert set(body.keys()) == {"ok", "service", "action"}
        assert body["ok"] is True
        assert body["service"] == "my-svc"
        assert body["action"] == "restarted"

    def test_logs_response_shape_matches_ops_api(self, control_plane):
        """ops-api returns {"logs": "...", "service": id}."""
        status, body = control_plane.route("GET", "/services/my-svc/logs")
        assert status == 200
        assert set(body.keys()) == {"logs", "service"}
        assert body["service"] == "my-svc"

    def test_dry_run_response_shape_matches_ops_api(self, control_plane):
        """ops-api returns {"would": action, "service": id}."""
        for action in ("start", "stop", "restart"):
            status, body = control_plane.route("POST", f"/services/my-svc/{action}", {"dry_run": True})
            assert status == 200
            assert set(body.keys()) == {"would", "service"}
            assert body["would"] == action
            assert body["service"] == "my-svc"

    def test_confirm_required_matches_ops_api(self, control_plane):
        """ops-api returns 400 with confirm message."""
        for action in ("start", "stop", "restart"):
            status, body = control_plane.route("POST", f"/services/my-svc/{action}", {})
            assert status == 400
            assert "confirm" in body["error"].lower()

    def test_service_id_extraction(self, control_plane):
        """Service ID is correctly extracted from path."""
        control_plane.route("POST", "/services/complex-service-name_123/start", {"dry_run": True})
        # Verify the service ID was parsed correctly
        status, body = control_plane.route("POST", "/services/complex-service-name_123/start", {"dry_run": True})
        assert body["service"] == "complex-service-name_123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
