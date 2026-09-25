"""Ordo: the config render engine, the control plane and the operator's host CLI.

Three layers, each a subpackage, with one import direction (tests/substrate/test_import_direction.py
enforces it, and fails on any import cycle between modules):

- `ordo.render`: the render engine and the contracts of what it renders (the compose file, the image
  record, the models volume). Imports nothing from `control` or `host`.
- `ordo.control`: the control plane that runs inside ops-controller (`ordo serve`): the HTTP API,
  the GPU scheduler, the broker, the audit log and the lease state. Imports `render` only.
- `ordo.host`: the commands that run on the operator's host (`ordo init`, `up`, `apply`, `build`,
  `fetch`, `secrets`, `remote`, `preflight`, `doctor`, ...). Imports `render` only; it reaches the
  control plane over HTTP, never by import.

`ordo/cli.py` parses arguments and dispatches to a layer; `ordo/secret_env.py` is the stdlib-only
secret reader every layer (and every service, by byte-identical copy) may use.
"""
from .render.catalog import Catalog, Model
from .render.config import Source
from .render.engine import RenderedConfig
from .render.hardware import HardwareProfile, detect

__all__ = ["Catalog", "Model", "Source", "HardwareProfile", "detect", "RenderedConfig"]
