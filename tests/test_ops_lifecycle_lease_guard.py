"""ops-controller's lifecycle verbs refuse to put a second tenant on a leased GPU.

The control plane hosts the GPU scheduler AND the start/restart/recreate/compose verbs. Before
this guard the verbs never asked the scheduler, so `POST /services/llamacpp/start` (or a
whole-stack `/compose/up`) during a render started the evicted resident beside the lease holder:
two tenants on one card, the 2026-08-08 host crash. The check lives here, once, in the process
that owns the lease, instead of in every caller.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Job, Scheduler

CONFIRM = {"confirm": True}


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def broker(backend):
    return Broker(Scheduler(total_vram_gb=32.0), backend)


@pytest.fixture
def control_plane(tmp_path, broker):
    return ControlPlane(source_path=tmp_path / "ordo.yaml", catalog=MagicMock(), registry=MagicMock(),
                        out_dir=tmp_path, scheduler=broker.scheduler, broker=broker)


@pytest.fixture
def leased(broker, backend):
    """A render holds the card and llamacpp has been evicted to make room for it."""
    broker.scheduler.cache_idle("llamacpp", 25)
    broker.request(Job("gate-comfyui", 20, "media"))
    assert "llamacpp" in broker.scheduler.evicted_residents
    backend.started.clear()
    backend.stopped.clear()
    return broker


@pytest.mark.parametrize("verb", ["start", "restart", "recreate"])
def test_an_evicted_resident_cannot_be_started_during_a_lease(control_plane, backend, leased, verb):
    status, body = control_plane.route("POST", f"/services/llamacpp/{verb}", CONFIRM)
    assert status == 409
    assert "gate-comfyui" in body["error"]
    assert backend.started == [] and backend.restarted == [] and backend.recreate_calls == []


def test_an_evicted_resident_cannot_be_restarted_by_container_name(control_plane, backend, leased):
    status, _ = control_plane.route("POST", "/containers/ordo-llamacpp-1/restart", CONFIRM)
    assert status == 409
    assert backend.container_restart_calls == []


@pytest.mark.parametrize("path", ["/compose/up", "/compose/restart"])
def test_a_whole_stack_start_is_refused_during_a_lease(control_plane, backend, leased, path):
    status, body = control_plane.route("POST", path, CONFIRM)
    assert status == 409
    assert backend.compose_up_calls == [] and backend.compose_restart_calls == []


def test_a_service_that_is_not_evicted_can_still_be_cycled_during_a_lease(control_plane, backend, leased):
    # The gate restarts its own wedged upstream (comfyui) while holding the lease; that must work.
    status, _ = control_plane.route("POST", "/services/comfyui/restart", CONFIRM)
    assert status == 200
    assert backend.restarted == ["comfyui"]


def test_stopping_an_evicted_resident_is_allowed(control_plane, backend, leased):
    status, _ = control_plane.route("POST", "/services/llamacpp/stop", CONFIRM)
    assert status == 200


def test_without_a_lease_the_resident_starts_normally(control_plane, backend):
    status, _ = control_plane.route("POST", "/services/llamacpp/start", CONFIRM)
    assert status == 200
    assert backend.started == ["llamacpp"]


@pytest.mark.parametrize("path,calls", [("/compose/up", "compose_up_calls"),
                                        ("/compose/restart", "compose_restart_calls"),
                                        ("/compose/down", "compose_down_calls")])
def test_compose_verbs_pass_the_named_service_through(control_plane, backend, path, calls):
    # A per-service call must not silently become a whole-stack one.
    status, _ = control_plane.route("POST", path, {"confirm": True, "service": "open-webui"})
    assert status == 200
    assert getattr(backend, calls) == ["open-webui"]


def test_a_named_compose_up_of_an_evicted_resident_is_refused(control_plane, backend, leased):
    status, _ = control_plane.route("POST", "/compose/up", {"confirm": True, "service": "llamacpp"})
    assert status == 409


def test_status_reports_whether_the_card_is_leased(broker, leased):
    assert broker.scheduler.status()["leased"] is True


def test_status_reports_an_idle_card_as_not_leased():
    assert Scheduler(total_vram_gb=32.0).status()["leased"] is False
