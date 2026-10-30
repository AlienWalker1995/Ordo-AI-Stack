"""`ordo doctor` judges every container: unhealthy, restarting, dead, crashed or stopped services fail it.

Before this, doctor (and so every `ordo apply`, which ends with it) reported a clean stack while couchdb,
livesync-bridge and both Langfuse services were unhealthy (2026-10-30)."""
from __future__ import annotations

from ordo.host import doctor


def _row(service, state="running", health="healthy", exit_code=0, restart_policy="unless-stopped", oneoff=False):
    return {"service": service, "state": state, "health": health, "exit_code": exit_code,
            "restart_policy": restart_policy, "oneoff": oneoff}


def test_a_healthy_stack_has_no_problems():
    assert doctor.container_problems([_row("couchdb"), _row("caddy", health=None)]) == []


def test_every_failure_shape_is_a_problem():
    rows = [_row("couchdb", health="unhealthy"), _row("livesync-bridge", state="restarting", health=None),
            _row("searxng", state="exited", health=None, exit_code=137),
            _row("qdrant", state="exited", health=None, exit_code=0),
            _row("broken", state="dead", health=None)]
    assert doctor.container_problems(rows) == [
        "broken is dead", "couchdb is unhealthy", "livesync-bridge is restarting",
        "qdrant is stopped", "searxng exited with code 137"]


def test_finished_one_shots_and_one_off_runs_are_not_problems():
    rows = [_row("model-gateway-keys", state="exited", health=None, restart_policy="on-failure"),
            _row("evals", state="exited", health=None, exit_code=1, oneoff=True)]
    assert doctor.container_problems(rows) == []


def test_a_one_shot_that_failed_is_a_problem():
    rows = [_row("model-gateway-keys", state="exited", health=None, exit_code=1, restart_policy="on-failure")]
    assert doctor.container_problems(rows) == ["model-gateway-keys exited with code 1"]


def test_the_check_waits_for_starting_healthchecks_then_judges_them():
    reads = iter([[_row("couchdb", health="starting")], [_row("couchdb", health="starting")],
                  [_row("couchdb", health="healthy")]])
    clock = iter(range(0, 1000, 10))
    slept = []
    ok, line = doctor.container_health_check("ordo", read=lambda p: next(reads), sleep=slept.append,
                                             clock=lambda: float(next(clock)))
    assert ok and line.startswith("containers: 1 running")
    assert len(slept) == 2


def test_a_healthcheck_still_starting_after_the_wait_fails():
    ok, line = doctor.container_health_check("ordo", settle_seconds=0,
                                             read=lambda p: [_row("couchdb", health="starting")],
                                             sleep=lambda s: None)
    assert not ok and "couchdb is still starting" in line


def test_unreadable_docker_fails_the_check():
    def broken(project):
        raise doctor.ContainerUnreadable("docker ps: daemon down")
    ok, line = doctor.container_health_check("ordo", read=broken)
    assert not ok and "cannot read them" in line
