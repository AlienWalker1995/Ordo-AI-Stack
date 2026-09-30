"""The scheduler's resident footprints follow the render, without an ops-controller restart.

The live defect (2026-09-30): after a model switch the scheduler kept registering llama.cpp at the
old model's footprint (27.5GB) while the new one held 30.2GB, because residents were registered
from the render once, at `ordo serve` startup, and never again. It thought 3.3GB of the card was
free when 1.7GB was, so a job that fit its arithmetic could have been admitted beside the resident.
A restart fixed it; a model switch did not. Three paths change the rendered model, and each must
leave the scheduler with the new footprint:

  * a control-plane apply (the dashboard's model switch, POST /model-config);
  * the host's `ordo apply`, which rewrites out/ and recreates llama.cpp without calling the
    control plane (the lease loop notices the new render);
  * a restart whose persisted state still holds an evicted resident at the old footprint.
"""
from __future__ import annotations

import yaml
from tests.substrate.test_control_apply import (
    CATALOG,
    HARDWARE,
    LARGE_CTX_MODEL,
    REGISTRY,
    SMALL_CTX_MODEL,
    RenderedStackBackend,
    _write_source,
)

from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.residents import ResidentFootprints, start_residents
from ordo.control.scheduler import Job, Scheduler
from ordo.control.scheduler_state import SchedulerStateStore
from ordo.render import gpu
from ordo.render.catalog import Catalog
from ordo.render.plugins import PluginRegistry

assert isinstance(CATALOG, Catalog) and isinstance(REGISTRY, PluginRegistry)


def _footprint(cp: ControlPlane, service: str = "llamacpp") -> float:
    """What the render the control plane reads now declares for `service`."""
    return gpu.primary_residents(cp._render().gpu_inventory())[service]


def _stack(tmp_path, model=SMALL_CTX_MODEL, state_path=None):
    """A control plane started the way `ordo serve` starts one: residents registered from the render,
    then the persisted state adopted."""
    source = tmp_path / "ordo.yaml"
    out = tmp_path / "out"
    _write_source(source, model=model)
    backend = RenderedStackBackend(out)
    scheduler = Scheduler(32)
    store = SchedulerStateStore(state_path) if state_path else None
    broker = Broker(scheduler, backend, state_store=store)
    cp = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=broker)
    cp._render().write(out)
    backend.create_all()
    start_residents(scheduler, broker, cp.residents, cp._render())
    return cp, backend, source, out


# --- the scheduler ----------------------------------------------------------------------------------------

def test_the_scheduler_adopts_a_new_footprint_for_an_idle_resident():
    sched = Scheduler(32)
    sched.cache_idle("llamacpp", 27.5)
    sched.cache_idle("llamacpp-embed", 1.0)

    changed = sched.update_resident_footprints({"llamacpp": 30.16, "llamacpp-embed": 1.0})

    assert changed == {"llamacpp": (27.5, 30.16)}
    assert sched.idle_cached == {"llamacpp": 30.16, "llamacpp-embed": 1.0}
    assert sched.free_vram_gb == 32 - 31.16
    assert sched.update_resident_footprints({"llamacpp": 30.16, "llamacpp-embed": 1.0}) == {}


def test_an_evicted_resident_takes_the_new_footprint_and_a_new_resident_is_registered():
    sched = Scheduler(32)
    sched.cache_idle("llamacpp", 27.5)
    sched.submit(Job("render", 31, "media"))
    sched.pump()
    assert sched.evicted_residents == {"llamacpp": 27.5}

    changed = sched.update_resident_footprints({"llamacpp": 30.16, "llamacpp-embed": 1.0})

    assert changed == {"llamacpp": (27.5, 30.16), "llamacpp-embed": (0.0, 1.0)}
    assert sched.evicted_residents == {"llamacpp": 30.16}   # restored at its true size
    assert sched.idle_cached == {"llamacpp-embed": 1.0}


# --- path 1: a control-plane apply ------------------------------------------------------------------------

def test_a_model_switch_through_the_control_plane_registers_the_new_footprint(tmp_path):
    cp, _backend, _source, _out = _stack(tmp_path)
    old = cp.scheduler.idle_cached["llamacpp"]
    status, body = cp.route("POST", "/model-config", {"model": LARGE_CTX_MODEL})
    assert status == 200, body
    new = _footprint(cp)
    assert new != old
    assert cp.scheduler.idle_cached["llamacpp"] == new


def test_a_dry_run_apply_changes_no_footprint(tmp_path):
    cp, _backend, source, out = _stack(tmp_path)
    old = cp.scheduler.idle_cached["llamacpp"]
    _write_source(source, model=LARGE_CTX_MODEL)
    cp._render().write(out)
    status, body = cp.route("POST", "/apply", {"dry_run": True})
    assert status == 200, body
    assert cp.scheduler.idle_cached["llamacpp"] == old


# --- path 2: the host's `ordo apply` (out/ rewritten on the host, the control plane not called) ------------

def test_the_lease_loop_adopts_a_render_the_host_wrote(tmp_path):
    cp, _backend, source, out = _stack(tmp_path)
    old = cp.scheduler.idle_cached["llamacpp"]
    assert cp.residents.adopt_if_render_changed() is None       # nothing changed: no re-render

    # the host edits the source and renders out/, as `ordo apply` does, then recreates llama.cpp
    _write_source(source, model=LARGE_CTX_MODEL)
    cp._render().write(out)

    changed = cp.residents.adopt_if_render_changed()
    new = _footprint(cp)
    assert changed == {"llamacpp": (old, new)}
    assert cp.scheduler.idle_cached["llamacpp"] == new
    assert cp.residents.adopt_if_render_changed() is None       # adopted once


# --- path 3: a restart with a stale persisted footprint ---------------------------------------------------

def test_a_restart_reconciles_a_persisted_evicted_footprint_with_the_render(tmp_path):
    state = tmp_path / "scheduler-state.json"
    cp, backend, source, out = _stack(tmp_path, state_path=state)
    cp.broker.request(Job("render", 31, "media", est_seconds=600))
    old = cp.scheduler.evicted_residents["llamacpp"]

    # while the resident is evicted the source moves to a bigger model (a hand edit, say), and
    # ops-controller restarts: the state file still holds the old footprint
    _write_source(source, model=LARGE_CTX_MODEL)
    scheduler = Scheduler(32)
    broker = Broker(scheduler, backend, state_store=SchedulerStateStore(state))
    cp2 = ControlPlane(source, CATALOG, REGISTRY, out, scheduler=scheduler, broker=broker)
    start_residents(scheduler, broker, cp2.residents, cp2._render())

    new = _footprint(cp2)
    assert new != old
    assert scheduler.running_ids == ["render"]
    assert scheduler.evicted_residents["llamacpp"] == new
    # and the file now says so, for the next restart
    assert yaml.safe_load(state.read_text(encoding="utf-8"))["evicted"]["llamacpp"] == new


def test_start_residents_registers_every_primary_resident_from_the_render(tmp_path):
    cp, *_ = _stack(tmp_path)
    assert cp.scheduler.idle_cached == gpu.primary_residents(cp._render().gpu_inventory())


def test_a_render_that_fails_keeps_the_footprints_it_had(tmp_path, capsys):
    cp, _backend, source, _out = _stack(tmp_path)
    before = cp.scheduler.idle_cached
    source.write_text("model: [not, valid\n", encoding="utf-8")
    assert cp.residents.adopt_if_render_changed() is None
    assert cp.scheduler.idle_cached == before
    assert "cannot re-read the render" in capsys.readouterr().err


def test_a_scheduler_without_a_broker_is_left_alone(tmp_path):
    source = tmp_path / "ordo.yaml"
    source.write_text(yaml.safe_dump({"hardware": HARDWARE, "plugins": "auto", "model": SMALL_CTX_MODEL}))
    cp = ControlPlane(source, CATALOG, REGISTRY, tmp_path / "out", scheduler=Scheduler(32), broker=None)
    assert cp.residents.adopt_if_render_changed() is None


def test_residents_type_is_the_control_planes(tmp_path):
    cp, *_ = _stack(tmp_path)
    assert isinstance(cp.residents, ResidentFootprints)
    assert MockBackend  # the fixtures above build on it
