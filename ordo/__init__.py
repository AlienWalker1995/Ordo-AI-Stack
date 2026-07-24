"""Ordo v2 substrate — config render engine (first slice)."""
from .catalog import Catalog, Model
from .config import Source
from .hardware import HardwareProfile, detect
from .render import RenderedConfig, render

__all__ = ["Catalog", "Model", "Source", "HardwareProfile", "detect", "RenderedConfig", "render"]
