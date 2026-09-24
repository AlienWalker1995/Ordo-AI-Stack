"""A fresh `ordo init --yes` renders a stack compose accepts with no hand-added variables.

The real headless flow (wizard with no answers, then render) on pinned hardware. Every
`${KEY:?}` the rendered compose references must be in the rendered .env, or `docker compose`
refuses the stack: that is how `plugins: auto` once enabled edge and memory-vault without the
site keys they cannot run without.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ordo import wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
# The module, not the `render` function the ordo package re-exports under the same name.
RENDER_MODULE = sys.modules["ordo.render"]

HARDWARE = {
    "no-gpu": {"gpus": [], "ram_gb": 16, "cpu_cores": 8},
    "one-32gb-gpu": {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32},
}


def _env_keys(env_file: Path) -> set[str]:
    keys = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if value.strip():
                keys.add(key.strip())
    return keys


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    return probe.returncode == 0


@pytest.mark.parametrize("hardware_name", sorted(HARDWARE))
def test_fresh_headless_install_renders_a_valid_stack(tmp_path, monkeypatch, hardware_name):
    hardware = HardwareProfile.from_spec(HARDWARE[hardware_name])
    monkeypatch.setattr(wizard, "detect", lambda: hardware)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: hardware)
    out = tmp_path / "out"

    result = wizard.run(CATALOG, REGISTRY, out, interactive=False, answers={},
                        host_root=tmp_path / "repo")
    render(Source.load(result.source_path), CATALOG, REGISTRY).write(out)

    compose_text = (out / "docker-compose.yml").read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", compose_text))
    rendered_env = _env_keys(out / ".env")
    assert required <= rendered_env, f"compose requires keys the rendered .env lacks: {required - rendered_env}"

    if not _docker_compose_available():
        return  # the static check above still ran; the engine check needs docker
    check = subprocess.run(
        ["docker", "compose", "-f", str(out / "docker-compose.yml"),
         "--env-file", str(out / ".env"), "--env-file", str(out / "secrets.env"), "config", "-q"],
        capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("hardware_name", sorted(HARDWARE))
def test_fresh_install_leaves_no_required_secret_blank(tmp_path, monkeypatch, hardware_name):
    """A local-only user answers no token prompts: every secret the default stack cannot run
    without is generated, and whatever stays blank is declared optional by its plugin. (The Funnel
    plugin once rode in on `plugins: auto` and demanded a Tailscale key from every fresh install.)"""
    hardware = HardwareProfile.from_spec(HARDWARE[hardware_name])
    monkeypatch.setattr(wizard, "detect", lambda: hardware)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: hardware)
    result = wizard.run(CATALOG, REGISTRY, tmp_path / "out", interactive=False, answers={},
                        host_root=tmp_path / "repo")
    rc = render(Source.load(result.source_path), CATALOG, REGISTRY)
    assert [key for key in result.blank_secret_keys if key not in rc.optional_secrets] == []
