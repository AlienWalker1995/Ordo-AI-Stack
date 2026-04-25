"""Pytest conftest: expose hyphenated ``ops-controller/`` directory as the
importable package ``ops_controller`` so test files can do
``from ops_controller.audit import AuditLog`` etc.

Python module names cannot contain hyphens, so we synthesize a package on
the fly. This only affects test runs — production code in this directory
imports siblings via plain ``from audit import AuditLog`` style imports.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_NAME = "ops_controller"


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
