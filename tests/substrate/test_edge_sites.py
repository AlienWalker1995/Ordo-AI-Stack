"""One declaration per UI edge port: the owning service's `edge_site: {port, upstream}`.

Before this, each UI's SSO-gated Caddy port was written down independently in the edge plugin's
`ports:` list, the Caddyfile, the dashboard card's `sso_port`, constants in the render engine and
the tailnet sidecar serve assets, and nothing made them agree. Now the owner's manifest declares
it once (`edge_site:` in plugin.yaml / dashboard.yaml; model-gateway, a core service with no
manifest, in ordo/render/engine.py) and everything else is derived from it or checked against it:

  * render publishes the edge listener's host ports from the ENABLED UIs' declarations,
  * LANGFUSE_PUBLIC_URL / PROXY_BASE_URL read the declared port,
  * the aggregated dashboard catalog carries each card's `sso_port` from its owner,
  * the Caddyfile (hand-written, so it stays readable) must have exactly one site per declared
    port pointing at the declared upstream, and no port site nobody declares
    (tests/test_caddyfile_invariants.py),
  * each tailnet sidecar's serve asset targets a declared port, one sidecar per port.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.dashboards import DashboardRegistry
from ordo.render.engine import (
    MODEL_GATEWAY_EDGE_SITE,
    aggregate_services_catalog,
    declared_edge_sites,
    render,
)
from ordo.render.plugins import EdgeSite, Plugin, PluginRegistry, PluginService

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(SERVICES)
DASHBOARDS = DashboardRegistry.load(SERVICES)

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
EDGE_SITE = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net",
             "CADDY_BIND": "0.0.0.0"}
BIND = "${CADDY_BIND:?CADDY_BIND must be set (non-empty)}"

# Every plugin that owns an edge site, plus what it needs to be enabled.
UI_PLUGINS = ["edge", "open-webui", "rag", "automation", "comfyui", "hermes-dashboard",
              "codebase-memory-ui", "langfuse"]


def _render(plugins, registry=REGISTRY, site=EDGE_SITE):
    src = Source.from_dict({"hardware": P_5090, "tier": "auto", "model": "auto",
                            "plugins": plugins, "site": site})
    return render(src, CATALOG, registry)


def _caddy_ports(rc) -> list[str]:
    return rc.compose_dict()["services"]["caddy"]["ports"]


def _with_langfuse_port(port: int) -> PluginRegistry:
    """The shipped registry with langfuse's ONE declaration moved to another port."""
    plugins: list[Plugin] = []
    for p in REGISTRY.plugins:
        if p.id == "langfuse":
            p = dataclasses.replace(p, edge_site=dataclasses.replace(p.edge_site, port=port))
        plugins.append(p)
    return PluginRegistry(plugins)


# ── the declarations ────────────────────────────────────────────────────────────

def test_the_declared_edge_sites_are_the_shipped_ui_ports():
    """Pins today's layout, so moving a UI is a reviewed change to one manifest line."""
    sites = declared_edge_sites(REGISTRY, DASHBOARDS)
    assert {owner: site.port for owner, site in sites.items()} == {
        "open-webui": 8443, "dashboard": 8444, "automation": 8445, "comfyui": 8446,
        "hermes-dashboard": 8447, "codebase-memory-ui": 8448, "model-gateway": 8449,
        "langfuse": 8450,
    }


def test_model_gateway_is_declared_once_in_the_engine():
    assert declared_edge_sites(REGISTRY, DASHBOARDS)["model-gateway"] is MODEL_GATEWAY_EDGE_SITE


def test_two_owners_cannot_claim_one_port():
    plugins = [dataclasses.replace(p, edge_site=EdgeSite(port=8443, upstream="x:1"))
               if p.id == "langfuse" else p for p in REGISTRY.plugins]
    with pytest.raises(ValueError, match="8443"):
        declared_edge_sites(PluginRegistry(plugins), DASHBOARDS)


@pytest.mark.parametrize("raw, message", [
    ({"port": 8443}, "upstream"),
    ({"upstream": "a:1"}, "port"),
    ({"port": "8443", "upstream": "a:1"}, "port"),
    ({"port": 70000, "upstream": "a:1"}, "port"),
    ({"port": 8443, "upstream": "no-port"}, "upstream"),
    ({"port": 8443, "upstream": "a:1", "path": "/"}, "exactly"),
])
def test_a_malformed_edge_site_is_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        EdgeSite.from_manifest(raw, "plugin 'x'")


def test_a_manifest_service_cannot_publish_host_ports_directly():
    """`ports:` would be a second, unchecked way to publish a UI port beside `edge_site`."""
    with pytest.raises(ValueError, match="edge_site"):
        PluginService.from_dict({"name": "x", "image": "x", "ports": ["0.0.0.0:8443:8443"]})


def test_the_edge_plugin_manifest_lists_no_ui_port():
    """The edge plugin owns only its own front door; UI ports come from their owners."""
    manifest = yaml.safe_load((SERVICES / "edge" / "plugin.yaml").read_text(encoding="utf-8"))
    caddy = next(s for s in manifest["services"] if s["name"] == "caddy")
    assert "ports" not in caddy
    assert caddy["edge_listener"]["ports"] == [443]


def test_a_local_port_is_the_same_number_as_the_edge_port():
    """`local_port.host` is documented as "the port Caddy serves <ui> on", so the URL an operator
    learns works in both access modes. Enforced, not copied by convention."""
    owners = [*REGISTRY.plugins, *DASHBOARDS.dashboards]
    checked = 0
    for owner in owners:
        local_ports = [owner.local_port] if hasattr(owner, "local_port") else [
            ps.local_port for ps in owner.services]
        for local_port in local_ports:
            if local_port is not None and owner.edge_site is not None:
                assert local_port.host == owner.edge_site.port, owner.id
                checked += 1
    assert checked >= 2, "open-webui and the dashboard both declare a local_port"


# ── render derives from them ────────────────────────────────────────────────────

def test_the_edge_publishes_its_front_door_plus_every_enabled_ui():
    rc = _render(UI_PLUGINS)
    ports = sorted(site.port for site in rc.edge_sites.values())
    assert _caddy_ports(rc) == [f"{BIND}:443:443"] + [f"{BIND}:{p}:{p}" for p in ports]
    assert ports == [8443, 8444, 8445, 8446, 8447, 8448, 8449, 8450]


def test_a_disabled_ui_publishes_no_edge_port():
    rc = _render([p for p in UI_PLUGINS if p != "langfuse"])
    assert f"{BIND}:8450:8450" not in _caddy_ports(rc)
    assert "langfuse" not in rc.edge_sites
    # core services are always enabled
    assert {"dashboard", "model-gateway"} <= set(rc.edge_sites)


def test_no_edge_listener_without_the_edge():
    rc = _render(["open-webui", "rag"], site={})
    assert "caddy" not in rc.compose_dict()["services"]


def test_moving_a_ui_is_one_manifest_edit():
    """Change langfuse's declaration only: the published port and its public URL follow."""
    rc = _render(UI_PLUGINS, registry=_with_langfuse_port(9450))
    assert f"{BIND}:9450:9450" in _caddy_ports(rc)
    assert f"{BIND}:8450:8450" not in _caddy_ports(rc)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://host.example.ts.net:9450"


def test_the_litellm_sso_origin_reads_the_model_gateway_declaration():
    rc = _render(["edge"])
    env = rc.compose_dict()["services"]["model-gateway"]["environment"]
    assert env["PROXY_BASE_URL"] == f"https://host.example.ts.net:{MODEL_GATEWAY_EDGE_SITE.port}"


def test_every_upstream_is_a_service_the_render_actually_runs():
    """A declared upstream names a rendered compose service, or loopback for a service living in
    the edge listener's network namespace (hermes-dashboard)."""
    rc = _render(UI_PLUGINS)
    services = rc.compose_dict()["services"]
    for owner, site in rc.edge_sites.items():
        host = site.upstream.rsplit(":", 1)[0]
        if host == "127.0.0.1":
            plugin = REGISTRY.get(owner)
            assert plugin is not None and any(
                ps.network_mode == "service:caddy" for ps in plugin.services), (
                f"{owner}: a loopback upstream is only reachable from inside caddy's netns")
        else:
            assert host in services, f"{owner}: upstream {site.upstream} is not a rendered service"


# ── the dashboard catalog derives from them ─────────────────────────────────────

def test_no_catalog_fragment_declares_an_sso_port():
    for frag in sorted(SERVICES.glob("*/catalog.json")):
        for card in json.loads(frag.read_text(encoding="utf-8"))["cards"]:
            assert "sso_port" not in card, (
                f"{frag}: `sso_port` is derived from the owner's `edge_site`; declare it there")


def test_the_aggregated_catalog_carries_each_owners_port():
    cards = {c["id"]: c for c in aggregate_services_catalog()["services"]}
    sites = declared_edge_sites(REGISTRY, DASHBOARDS)
    assert cards["langfuse"]["sso_port"] == sites["langfuse"].port
    assert cards["model-gateway"]["sso_port"] == sites["model-gateway"].port
    assert cards["webui"]["sso_port"] == sites["open-webui"].port
    assert cards["n8n"]["sso_port"] == sites["automation"].port
    assert cards["comfyui"]["sso_port"] == sites["comfyui"].port
    assert cards["hermes"]["sso_port"] == sites["hermes-dashboard"].port
    assert cards["codebase-memory-ui"]["sso_port"] == sites["codebase-memory-ui"].port
    # a backend with no edge site gets none
    assert "sso_port" not in cards["qdrant"]


# ── the tailnet sidecars front exactly the declared ports ───────────────────────

def _sidecar_ports() -> dict[str, int]:
    """TS_HOSTNAME -> the Caddy port its serve asset proxies to."""
    manifest = yaml.safe_load((SERVICES / "tailnet-names" / "plugin.yaml").read_text(encoding="utf-8"))
    ports: dict[str, int] = {}
    for svc in manifest["services"]:
        label = svc["env"]["TS_HOSTNAME"]
        serve = json.loads((ROOT / "assets" / "tailscale-serve" / f"{label}.json").read_text(encoding="utf-8"))
        proxy = serve["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]["/"]["Proxy"]
        ports[label] = int(proxy.rsplit(":", 1)[1])
    return ports


def test_each_sidecar_fronts_one_declared_port_and_every_port_has_one():
    ports = _sidecar_ports()
    declared = sorted(site.port for site in declared_edge_sites(REGISTRY, DASHBOARDS).values())
    assert sorted(ports.values()) == declared


def test_a_cards_sidecar_fronts_that_cards_port():
    cards = aggregate_services_catalog()["services"]
    ports = _sidecar_ports()
    for card in cards:
        if card.get("tailnet_label") and card.get("sso_port"):
            assert ports[card["tailnet_label"]] == card["sso_port"], card["id"]
