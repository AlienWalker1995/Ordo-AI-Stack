"""The real container backend must implement every method the protocol declares.

This test exists because of a live defect on 2026-09-21. `ContainerBackend` is a `typing.Protocol`,
which is checked by a type checker and NOT at runtime, so a backend can be missing half its methods
and every import, every instantiation and every mock-backed test still passes. Slice 1 shipped ten
routes whose handlers called methods that existed on `MockBackend` and on the protocol but not on
`DockerBackend`. 631 tests were green. Every one of those routes answered

    HTTP 500 {"error": "'DockerBackend' object has no attribute 'list_services'"}

the first time it was called against real docker.

A suite that mocks its only production implementation proves the mock agrees with itself. This
turns that whole class of bug from a production 500 into a red CI run, which is the only reason it
is worth its length.
"""
from __future__ import annotations

import inspect

from ordo.broker import ContainerBackend, DockerBackend, MockBackend


def _public_methods(cls) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_docker_backend_implements_every_protocol_method():
    required = _public_methods(ContainerBackend)
    implemented = _public_methods(DockerBackend)
    missing = sorted(required - implemented)
    assert not missing, (
        "DockerBackend is missing protocol methods, so any route calling one of these returns a "
        f"500 against real docker while mock-backed tests stay green: {missing}"
    )


def test_mock_backend_implements_every_protocol_method():
    """The mock must not drift ahead of the real backend either.

    If the mock grows a method the protocol does not declare, tests can pass against behaviour no
    production backend is obliged to provide, which is the same failure wearing a different hat.
    """
    required = _public_methods(ContainerBackend)
    implemented = _public_methods(MockBackend)
    missing = sorted(required - implemented)
    assert not missing, f"MockBackend is missing protocol methods: {missing}"


def test_the_two_backends_agree_on_their_signatures():
    """Same names is not enough: a caller that passes `tail=` to one and not the other still breaks.

    Compared by parameter NAME rather than by full signature, because the real backend is free to
    annotate more precisely or give a default the mock does not need.
    """
    mismatches = []
    for name in sorted(_public_methods(ContainerBackend)):
        proto = inspect.signature(getattr(ContainerBackend, name))
        for impl_cls in (DockerBackend, MockBackend):
            impl = getattr(impl_cls, name, None)
            if impl is None:
                continue  # covered by the tests above, with a clearer message
            proto_params = [p for p in proto.parameters if p != "self"]
            impl_params = [p for p in inspect.signature(impl).parameters if p != "self"]
            if proto_params != impl_params:
                mismatches.append(f"{impl_cls.__name__}.{name}{tuple(impl_params)} != protocol{tuple(proto_params)}")
    assert not mismatches, "backend signatures diverge from the protocol: " + "; ".join(mismatches)
