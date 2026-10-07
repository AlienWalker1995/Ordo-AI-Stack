"""The netns repair sweep's rules (ordo/control/netns_repair.py).

The reproduction of the 2026-10-06 failure against fake and real docker lives in
test_backend_contract.py. These pin the decisions: which stopped member is repaired, which is left
alone, the per-container budget, a managed project's members, and what /metrics reports.
"""
from __future__ import annotations

import subprocess

from ordo.control import metrics
from ordo.control.broker import MockBackend, netns_member_rows
from ordo.control.managed import RestartBudget
from ordo.control.netns_repair import NetnsRepair, is_orphan, repairable

ERROR = "cannot join network namespace of a non running container: container ordo-caddy-1 is exited"


def _row(**over):
    row = {"service": "tailnet-dash", "name": "ordo-tailnet-dash-1", "state": "exited", "exit_code": 128,
           "error": ERROR, "restart_policy": "unless-stopped", "owner": "ordo-caddy-1", "owner_state": "running"}
    row.update(over)
    return row


def test_a_member_docker_failed_to_start_is_an_orphan():
    assert is_orphan(_row())
    assert is_orphan(_row(state="created"))


def test_a_running_or_deliberately_stopped_member_is_not():
    assert not is_orphan(_row(state="running"))
    assert not is_orphan(_row(error="", exit_code=143)), "docker stop leaves no namespace error"
    assert not is_orphan(_row(restart_policy="no")), "a one-shot is meant to stay down"


def test_an_orphan_is_repaired_only_while_its_owner_can_take_it():
    assert repairable(_row(), own_project=True)
    assert not repairable(_row(owner_state="exited"), own_project=True)
    # Ordo's own members are recreated through compose, which joins the CURRENT owner container.
    assert repairable(_row(owner_state="missing"), own_project=True)
    # A managed project's member is only restarted: it needs the very owner it was created against.
    assert not repairable(_row(owner_state="missing"), own_project=False)


class _Scripted:
    """A backend whose netns rows are scripted, recording the repairs asked of it."""

    project = "ordo"

    def __init__(self, rows, fail=False):
        self.rows = rows
        self.fail = fail
        self.recreated: list[str] = []
        self.foreign_restarted: list[tuple[str, str]] = []

    def netns_rows(self, project=None):
        return self.rows.get(project or self.project, [])

    def recreate_service(self, service):
        self.recreated.append(service)
        if self.fail:
            raise subprocess.CalledProcessError(1, ["docker", "compose"], stderr="no space left on device")

    def foreign_restart(self, project, name):
        self.foreign_restarted.append((project, name))


def _repair(backend, managed=(), budget=None):
    return NetnsRepair(backend, "ordo", lambda: list(managed), budget=budget, log=lambda _line: None)


def test_own_members_are_recreated_and_managed_members_restarted():
    backend = _Scripted({"ordo": [_row()],
                         "nas-stack": [_row(service="qbittorrent", name="qbittorrent", owner="gluetun")]})
    assert _repair(backend, ["nas-stack"]).sweep() == ["ordo/ordo-tailnet-dash-1", "nas-stack/qbittorrent"]
    assert backend.recreated == ["tailnet-dash"]
    assert backend.foreign_restarted == [("nas-stack", "qbittorrent")]


def test_an_owner_that_is_down_leaves_the_member_counted_as_an_orphan():
    repair = _repair(_Scripted({"ordo": [_row(owner_state="exited")]}))
    assert repair.sweep() == []
    assert repair.stats() == {"repaired": 0, "failed": 0, "orphans": 1}


def test_a_failed_repair_is_counted_and_the_sweep_goes_on():
    repair = _repair(_Scripted({"ordo": [_row(), _row(service="tailnet-chat", name="ordo-tailnet-chat-1")]},
                               fail=True))
    assert repair.sweep() == []
    assert repair.stats() == {"repaired": 0, "failed": 2, "orphans": 2}


def test_the_budget_stops_a_member_that_keeps_falling_over():
    now = [0.0]
    budget = RestartBudget(limit=3, window_seconds=3600, clock=lambda: now[0])
    backend = _Scripted({"ordo": [_row()]})
    repair = _repair(backend, budget=budget)
    for _ in range(3):
        assert repair.sweep() == ["ordo/ordo-tailnet-dash-1"]
    assert repair.sweep() == [], "a fourth repair inside the hour is left to the operator"
    assert repair.stats()["orphans"] == 1
    now[0] = 3601.0
    assert repair.sweep() == ["ordo/ordo-tailnet-dash-1"], "the budget frees up after the window"


def test_an_unreadable_managed_project_does_not_stop_ordos_own_sweep():
    class Broken(_Scripted):
        def netns_rows(self, project=None):
            if project:
                raise subprocess.CalledProcessError(1, ["docker"], stderr="boom")
            return super().netns_rows(project)

    backend = Broken({"ordo": [_row()]})
    assert _repair(backend, ["nas-stack"]).sweep() == ["ordo/ordo-tailnet-dash-1"]


def test_inspect_documents_parse_into_rows():
    """The real backend's parser: a gluetun-style VPN member and its owner, as `docker inspect`
    reports them after a daemon restart."""
    owner = {"Id": "aaa", "Name": "/gluetun", "State": {"Status": "running"}, "HostConfig": {"NetworkMode": "bridge"},
             "Config": {"Labels": {"com.docker.compose.service": "gluetun"}}}
    member = {"Id": "bbb", "Name": "/qbittorrent",
              "State": {"Status": "exited", "ExitCode": 128, "Error": ERROR},
              "HostConfig": {"NetworkMode": "container:aaa", "RestartPolicy": {"Name": "unless-stopped"}},
              "Config": {"Labels": {"com.docker.compose.service": "qbittorrent"}}}
    rows = netns_member_rows([owner, member], lambda ref: "missing")
    assert rows == [{"service": "qbittorrent", "name": "qbittorrent", "state": "exited", "exit_code": 128,
                     "error": ERROR, "restart_policy": "unless-stopped", "owner": "gluetun",
                     "owner_state": "running"}]
    gone = netns_member_rows([member], lambda ref: "missing")
    assert gone[0]["owner_state"] == "missing" and gone[0]["owner"] == "aaa"


def test_a_managed_project_in_the_fake_is_parsed_like_the_real_one():
    backend = MockBackend()
    member = {"Id": "bbb", "Name": "/prowlarr", "State": {"Status": "exited", "ExitCode": 128, "Error": ERROR},
              "HostConfig": {"NetworkMode": "container:aaa", "RestartPolicy": {"Name": "unless-stopped"}},
              "Config": {"Labels": {"com.docker.compose.service": "prowlarr"}}}
    owner = {"Id": "aaa", "Name": "/gluetun", "State": {"Status": "running"}, "HostConfig": {}, "Config": {}}
    backend.foreign["nas-stack"] = {"prowlarr": ({"name": "prowlarr"}, member), "gluetun": ({"name": "gluetun"}, owner)}
    repair = NetnsRepair(backend, "ordo", lambda: ["nas-stack"], log=lambda _line: None)
    assert repair.sweep() == ["nas-stack/prowlarr"]
    assert backend.foreign_restarts == [("nas-stack", "prowlarr")]


def test_metrics_report_the_sweep():
    text = metrics.render(metrics.Inputs(netns_repair={"repaired": 2, "failed": 1, "orphans": 1}))
    assert 'ordo_netns_repairs_total{result="repaired"} 2' in text
    assert 'ordo_netns_repairs_total{result="failed"} 1' in text
    assert "ordo_netns_orphans 1" in text
    assert "ordo_netns" not in metrics.render(metrics.Inputs()), "not wired = not reported, never zero"
