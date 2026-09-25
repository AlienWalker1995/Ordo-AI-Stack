"""Local access without the edge: the UIs publish loopback-only host ports, declared in manifests.

A local-only install (no Tailscale, no Google) has no Caddy, so without these ports it has no way
into any UI. When the edge IS enabled, Caddy is the one front door and these ports stay closed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.dashboards import Dashboard
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry, PluginService

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


# --------------------------------------------------------------------------- #
# The dashboard's local sign-in secret follows the same edge on/off derivation
# --------------------------------------------------------------------------- #

LOCAL_LOGIN_SECRET = "DASHBOARD_LOCAL_LOGIN_TOKEN"


def _rendered(site: dict):
    source = Source.from_dict({"hardware": HARDWARE, "model": "auto", "plugins": "auto", "site": site})
    return render(source, CATALOG, REGISTRY)


def _services_reading(compose: dict, key: str) -> list[str]:
    """Services that get `key` in their environment, as a value or as a file (`<key>_FILE`)."""
    return sorted(name for name, svc in compose["services"].items()
                  if {key, f"{key}_FILE"} & set(svc.get("environment") or {}))


def test_without_the_edge_only_the_dashboard_gets_the_local_sign_in_secret():
    rc = _rendered(HOST_PATHS)
    compose = rc.compose_dict()
    assert _services_reading(compose, LOCAL_LOGIN_SECRET) == ["dashboard"]
    # a file under /run/secrets, never the value
    dashboard = compose["services"]["dashboard"]
    assert dashboard["environment"][f"{LOCAL_LOGIN_SECRET}_FILE"] == f"/run/secrets/{LOCAL_LOGIN_SECRET.lower()}"
    assert LOCAL_LOGIN_SECRET not in dashboard["environment"]
    assert LOCAL_LOGIN_SECRET in rc.required_secrets
    assert LOCAL_LOGIN_SECRET not in rc.optional_secrets


def test_with_the_edge_no_service_gets_the_local_sign_in_secret():
    rc = _rendered({**HOST_PATHS, **EDGE_KEYS})
    assert _services_reading(rc.compose_dict(), LOCAL_LOGIN_SECRET) == []
    assert LOCAL_LOGIN_SECRET not in rc.required_secrets


def test_the_local_sign_in_secret_is_declared_by_the_dashboard_manifest():
    import yaml

    raw = yaml.safe_load((ROOT / "services/dashboard/dashboard.yaml").read_text(encoding="utf-8"))
    assert Dashboard.from_dict(raw).local_login_secret == LOCAL_LOGIN_SECRET


def test_a_local_sign_in_secret_needs_a_local_port():
    with pytest.raises(ValueError):
        Dashboard.from_dict({"id": "d", "image": "d", "local_login_secret": LOCAL_LOGIN_SECRET})


def test_the_local_sign_in_secret_is_generated_not_asked_for():
    from ordo.host import wizard

    value = wizard.generator_for(LOCAL_LOGIN_SECRET)()
    assert len(value) >= 32


def test_the_manifest_records_the_dashboard_sign_in_only_without_the_edge():
    assert _rendered(HOST_PATHS).manifest()["dashboard_sign_in"] == {
        "url": "http://127.0.0.1:8444", "secret": LOCAL_LOGIN_SECRET}
    assert "dashboard_sign_in" not in _rendered({**HOST_PATHS, **EDGE_KEYS}).manifest()


# --------------------------------------------------------------------------- #
# `ordo up` / `ordo init`: the operator gets a sign-in link, and an older local install its secret
# --------------------------------------------------------------------------- #


def _out_dir(tmp_path: Path, site: dict, secrets: str) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.json").write_text(json.dumps(_rendered(site).manifest()), encoding="utf-8")
    (out / "secrets.env").write_text(secrets, encoding="utf-8")
    return out


def test_up_mints_a_missing_local_sign_in_secret_and_keeps_the_rest(tmp_path):
    from ordo.host import cli_secrets, parity

    out = _out_dir(tmp_path, HOST_PATHS, "OPS_CONTROLLER_TOKEN=keep-me\n")
    assert cli_secrets._ensure_local_sign_in_secret(out) is True
    values = parity.load_env(str(out / "secrets.env"))
    assert values["OPS_CONTROLLER_TOKEN"] == "keep-me"
    assert len(values[LOCAL_LOGIN_SECRET]) >= 32
    minted = values[LOCAL_LOGIN_SECRET]
    assert cli_secrets._ensure_local_sign_in_secret(out) is False    # idempotent: an existing value stays
    assert parity.load_env(str(out / "secrets.env"))[LOCAL_LOGIN_SECRET] == minted


def test_up_leaves_secrets_alone_with_the_edge(tmp_path):
    from ordo.host import cli_secrets

    out = _out_dir(tmp_path, {**HOST_PATHS, **EDGE_KEYS}, "OPS_CONTROLLER_TOKEN=keep-me\n")
    assert cli_secrets._ensure_local_sign_in_secret(out) is False
    assert (out / "secrets.env").read_text(encoding="utf-8") == "OPS_CONTROLLER_TOKEN=keep-me\n"


def test_the_sign_in_link_carries_the_token_in_the_fragment(tmp_path):
    from ordo.host import cli_secrets

    out = _out_dir(tmp_path, HOST_PATHS, f"{LOCAL_LOGIN_SECRET}=tok-123\n")
    # The fragment never reaches the server (no access log, no Referer); the SPA posts it once.
    assert cli_secrets._dashboard_sign_in_link(out) == "http://127.0.0.1:8444/#sign-in=tok-123"


def test_no_sign_in_link_with_the_edge(tmp_path):
    from ordo.host import cli_secrets

    out = _out_dir(tmp_path, {**HOST_PATHS, **EDGE_KEYS}, f"{LOCAL_LOGIN_SECRET}=tok-123\n")
    assert cli_secrets._dashboard_sign_in_link(out) is None
