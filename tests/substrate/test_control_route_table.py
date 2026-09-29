"""The ops-controller route table (ordo/control/routes.py) is data, so it can be checked as data.

The principal allowlist (ordo/control/principals.py) grants routes by (method, path template). A
grant that names a path shape the table does not serve grants nothing and hides a typo; the
behavioural check (`test_every_allowlisted_route_exists`) calls each allowed path, and this one
checks the grant against the table entry that must serve it.
"""
from __future__ import annotations

import pytest

from ordo.control import principals, routes
from ordo.control.api import ControlPlane
from ordo.control.broker import Broker, MockBackend
from ordo.control.scheduler import Scheduler

SERVED = {(entry.method, entry.pattern.template) for entry in routes.ROUTES}


@pytest.mark.parametrize("grant", principals.HERMES_ROUTES, ids=lambda g: f"{g.method} {g.template}")
def test_every_allowlisted_route_is_an_entry_in_the_table(grant):
    assert (grant.method, routes.Template(grant.template).template) in SERVED


def test_no_two_entries_serve_the_same_route():
    served = [(entry.method, entry.pattern.template) for entry in routes.ROUTES]
    assert len(served) == len(set(served))


@pytest.mark.parametrize("method, path, params", [
    ("GET", "/status", {}),
    ("get", "/status", {}),
    ("POST", "/services/llamacpp/start", {"id": "llamacpp"}),
    ("POST", "/services/a/b/start", {"id": "a/b"}),          # a Span id may carry a slash
    ("POST", "/services/start", {"id": ""}),                 # and may be empty
    ("GET", "/containers/ordo-llamacpp-1/logs", {"name": "ordo-llamacpp-1"}),
    ("GET", "/containers/ordo-llamacpp-1", {"name": "ordo-llamacpp-1"}),
    ("GET", "/projects/side/containers/web-1/logs", {"project": "side", "name": "web-1"}),
])
def test_a_path_resolves_to_its_parameters(method, path, params):
    found = routes.find(method, path)
    assert found is not None
    assert found[1] == params


@pytest.mark.parametrize("method, path", [
    ("GET", "/containers/a/b"),                  # inspect takes one segment
    ("GET", "/projects//containers"),            # a Template placeholder is never empty
    ("POST", "/projects/side/containers/a/b/restart"),
    ("DELETE", "/services/llamacpp/restart"),
    ("GET", "/nope"),
])
def test_a_path_nothing_serves_resolves_to_none(method, path):
    assert routes.find(method, path) is None


def test_the_logs_route_is_matched_before_the_inspect_route():
    """`GET /containers/logs` is the logs route with an empty name, never an inspect of a container
    named `logs`: the logs entry must precede the inspect entry, which would also match it."""
    entry, params = routes.find("GET", "/containers/logs")
    assert (entry.pattern.template, params) == ("/containers/{}/logs", {"name": ""})


def test_containers_logs_reaches_the_logs_handler(tmp_path):
    backend = MockBackend({"services": {"llamacpp": {"image": "llama"}}})
    scheduler = Scheduler(32)
    cp = ControlPlane(tmp_path / "ordo.yaml", None, None, tmp_path / "out", scheduler=scheduler,
                      broker=Broker(scheduler, backend))
    cp.route("GET", "/containers/logs")
    assert backend.container_log_requests == [("", 100)]
    assert backend.inspect_requests == []
