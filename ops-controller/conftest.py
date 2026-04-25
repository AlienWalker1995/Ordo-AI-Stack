"""Pytest conftest: expose hyphenated ``ops-controller/`` directory as the
importable package ``ops_controller`` so test files can do
``from ops_controller.audit import AuditLog`` etc.

Python module names cannot contain hyphens, so we synthesize a package on
the fly. This only affects test runs — production code in this directory
imports siblings via plain ``from audit import AuditLog`` style imports.

Also stubs out ``docker`` (the SDK) so ``main.py`` can be imported on dev
boxes that don't have the docker-py package installed.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Stub ``docker`` SDK before any test imports ``ops_controller.main``.
if "docker" not in sys.modules:
    docker_stub = MagicMock()

    # Real ``docker.errors.NotFound`` is a real exception type — endpoints
    # do ``except docker.errors.NotFound``. Provide a real class.
    class _NotFound(Exception):
        pass

    errors_mod = types.ModuleType("docker.errors")
    errors_mod.NotFound = _NotFound  # type: ignore[attr-defined]
    docker_stub.errors = errors_mod
    sys.modules["docker"] = docker_stub
    sys.modules["docker.errors"] = errors_mod

    # Make ``docker.from_env()`` return a client whose ``containers.list``
    # yields an empty iterable and whose ``containers.get`` raises NotFound
    # by default. Individual tests that need specific containers can
    # monkeypatch ``_dc`` / ``_docker_client``.
    _client = MagicMock()
    _client.containers.list.return_value = []

    def _get_raises(name):  # noqa: ANN001
        raise _NotFound(f"container {name} not found (stubbed)")

    _client.containers.get.side_effect = _get_raises
    docker_stub.from_env.return_value = _client

_HERE = Path(__file__).resolve().parent
_PKG_NAME = "ops_controller"

# Also let production-style imports (``from audit import AuditLog``) work in
# tests, so ``main.py`` doesn't need an import shim.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _install_synthetic_package() -> None:
    if _PKG_NAME in sys.modules:
        return
    pkg = types.ModuleType(_PKG_NAME)
    pkg.__path__ = [str(_HERE)]  # type: ignore[attr-defined]
    pkg.__file__ = str(_HERE / "__init__.py")
    # Register a loader hook so ``import ops_controller.audit`` etc. resolves
    # to files in this directory.

    class _DirFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):  # noqa: D401, ANN001
            if not fullname.startswith(_PKG_NAME + "."):
                return None
            sub = fullname.split(".", 1)[1]
            candidate = _HERE / f"{sub}.py"
            if not candidate.is_file():
                return None
            return importlib.util.spec_from_file_location(fullname, candidate)

    sys.modules[_PKG_NAME] = pkg
    sys.meta_path.insert(0, _DirFinder())


_install_synthetic_package()
