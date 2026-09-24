"""Local access without the edge: the UIs publish loopback-only host ports, declared in manifests.

A local-only install (no Tailscale, no Google) has no Caddy, so without these ports it has no way
into any UI. When the edge IS enabled, Caddy is the one front door and these ports stay closed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.dashboards import Dashboard
from ordo.plugins import PluginRegistry, PluginService
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

HARDWARE = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
HOST_PATHS = {"BASE_PATH": "/srv/ordo", "DATA_PATH": "/srv/ordo/data",
              "MEMORY_VAULT_PATH": "/srv/ordo/data/memory-vault"}
EDGE_KEYS = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "ordo.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net"}

# The UIs a local-only user reaches, and the loopback address each one publishes.
LOCAL_UIS = {"open-webui": "127.0.0.1:8443:8080", "dashboard": "127.0.0.1:8444:8080"}


def _compose(site: dict) -> dict:
    source = Source.from_dict({"hardware": HARDWARE, "model": "auto", "plugins": "auto", "site": site})
    return render(source, CATALOG, REGISTRY).compose_dict()


def _published(compose: dict) -> dict[str, list[str]]:
    return {name: list(svc["ports"]) for name, svc in compose["services"].items() if svc.get("ports")}


def test_without_the_edge_the_uis_publish_loopback_ports():
    compose = _compose(HOST_PATHS)
    assert "caddy" not in compose["services"]
    published = _published(compose)
    for service, port in LOCAL_UIS.items():
        assert published.get(service) == [port]


def test_without_the_edge_nothing_is_published_beyond_loopback():
    for service, ports in _published(_compose(HOST_PATHS)).items():
        for port in ports:
            assert port.startswith("127.0.0.1:"), f"{service} publishes {port} off loopback"


def test_with_the_edge_caddy_is_the_only_front_door():
    compose = _compose({**HOST_PATHS, **EDGE_KEYS})
    published = _published(compose)
    assert "caddy" in published
    for service in LOCAL_UIS:
        assert service not in published, f"{service} publishes a port beside the edge"


def test_local_ports_come_from_the_manifests():
    webui = next(ps for ps in REGISTRY.get("open-webui").services if ps.name == "open-webui")
    assert (webui.local_port.host, webui.local_port.container) == (8443, 8080)
    dashboard = Dashboard.from_dict(
        __import__("yaml").safe_load((ROOT / "services/dashboard/dashboard.yaml").read_text(encoding="utf-8")))
    assert (dashboard.local_port.host, dashboard.local_port.container) == (8444, 8080)


@pytest.mark.parametrize("bad", [
    {"host": "0.0.0.0:8443", "container": 8080},   # a manifest names a port, never an address
    {"host": 8443},                                 # both halves are required
    {"host": 70000, "container": 8080},             # out of range
    "8443:8080",                                    # the mapping form only
])
def test_a_local_port_cannot_name_an_address_or_be_malformed(bad):
    with pytest.raises(ValueError):
        PluginService.from_dict({"name": "x", "image": "x:1", "local_port": bad})


def test_the_chat_card_links_to_the_published_local_port():
    card = json.loads((ROOT / "services/open-webui/catalog.json").read_text(encoding="utf-8"))["cards"][0]
    host, host_port, _ = LOCAL_UIS["open-webui"].split(":")
    assert card["port"] == int(host_port)
    assert card["url"] == f"http://{host}:{host_port}"
