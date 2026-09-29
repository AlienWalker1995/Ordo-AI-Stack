"""The alerting wiring: the monitoring plugin's Alertmanager, the rules Prometheus loads, the secrets
Alertmanager delivers with, and ops-controller's view of the edge certificate.

The tracked config files (monitoring/) and the rendered compose name the same paths, services and
ports from two sides; these tests hold each pair together, so a rename on one side fails here
rather than as an alert that silently never fires or never arrives.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ordo.render import alerting, compose
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry
from ordo.render.secret_files import secret_files_in

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
PROMETHEUS = yaml.safe_load((ROOT / "monitoring" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
ALERTMANAGER_TEXT = (ROOT / "monitoring" / "alertmanager" / "alertmanager.yml").read_text(encoding="utf-8")
ALERTMANAGER = yaml.safe_load(ALERTMANAGER_TEXT)
RULES = yaml.safe_load((ROOT / "monitoring" / "prometheus" / "rules" / "ordo-alerts.yml").read_text(encoding="utf-8"))
HW = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-aaaa"}], "ram_gb": 128, "cpu_cores": 32}
EDGE_SITE = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net", "SSO_ALLOWED_EMAILS": "me@example.com"}
DELIVERY_KEYS = [alerting.DISCORD_WEBHOOK_KEY, alerting.HEARTBEAT_KEY]


def _render(plugins, site=None):
    return render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": plugins, "site": site or {}}),
                  CATALOG, REGISTRY)


@pytest.fixture(scope="module")
def monitored():
    rc = _render(["monitoring"])
    return rc, rc.compose_dict()


def _mount_target(service: dict, source_suffix: str) -> str:
    matches = [v.split(":")[-2] for v in service["volumes"] if v.split(":/")[0].endswith(source_suffix)]
    assert len(matches) == 1, f"expected one mount of {source_suffix}: {service['volumes']}"
    return matches[0]


# --------------------------------------------------------------------------- #
# The rendered services.
# --------------------------------------------------------------------------- #


def test_alertmanager_is_rendered_pinned_by_digest(monitored):
    _, doc = monitored
    am = doc["services"]["alertmanager"]
    assert am["image"].startswith("prom/alertmanager:v") and "@sha256:" in am["image"]
    assert am["profiles"] == ["monitoring"]
    assert "--config.file=/etc/alertmanager/alertmanager.yml" in am["command"]


def test_the_delivery_urls_reach_alertmanager_only_as_files(monitored):
    rc, doc = monitored
    assert sorted(key for service, key in secret_files_in(doc) if service == "alertmanager") == sorted(DELIVERY_KEYS)
    assert not any(key in DELIVERY_KEYS for service, key in secret_files_in(doc) if service != "alertmanager")
    # Declared, so `ordo secrets list` shows them, and optional, so a fresh install still comes up.
    assert set(DELIVERY_KEYS) <= set(rc.required_secrets)
    assert set(DELIVERY_KEYS) <= set(rc.optional_secrets)


def test_alertmanager_reads_the_files_the_render_mounts(monitored):
    """alertmanager.yml names each URL file by path; the render mounts each key at /run/secrets/<key>."""
    _, doc = monitored
    mounted = {v.split(":")[-2] for v in doc["services"]["alertmanager"]["volumes"]}
    receivers = {r["name"]: r for r in ALERTMANAGER["receivers"]}
    discord = receivers["discord"]["discord_configs"][0]["webhook_url_file"]
    heartbeat = receivers["heartbeat"]["webhook_configs"][0]["url_file"]
    assert discord == f"/run/secrets/{alerting.DISCORD_WEBHOOK_KEY.lower()}" and discord in mounted
    assert heartbeat == f"/run/secrets/{alerting.HEARTBEAT_KEY.lower()}" and heartbeat in mounted
    assert _mount_target(doc["services"]["alertmanager"], "/monitoring/alertmanager/alertmanager.yml") == \
        "/etc/alertmanager/alertmanager.yml"


def test_prometheus_loads_the_rules_directory_the_render_mounts(monitored):
    _, doc = monitored
    target = _mount_target(doc["services"]["prometheus"], "/monitoring/prometheus/rules")
    assert PROMETHEUS["rule_files"] == [f"{target}/*.yml"]


def test_prometheus_sends_to_the_rendered_alertmanager_and_scrapes_ops_controller(monitored):
    _, doc = monitored
    assert PROMETHEUS["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["alertmanager:9093"]
    assert "alertmanager" in doc["services"] and "ops-controller" in doc["services"]
    jobs = {j["job_name"]: j for j in PROMETHEUS["scrape_configs"]}
    assert jobs["ops-controller"]["static_configs"][0]["targets"] == ["ops-controller:9000"]
    assert jobs["ops-controller"]["metrics_path"] == "/metrics"


def test_the_gpu_exporter_reports_the_power_limit_the_power_rule_reads(monitored):
    _, doc = monitored
    fields = doc["services"]["gpu-exporter"]["command"][0].removeprefix("--query-field-names=").split(",")
    assert "enforced.power.limit" in fields and "power.draw" in fields and "temperature.gpu" in fields
    exprs = " ".join(r["expr"] for g in RULES["groups"] for r in g["rules"])
    assert "nvidia_smi_enforced_power_limit_watts" in exprs


def test_a_stack_without_monitoring_renders_no_alertmanager_and_no_delivery_keys():
    rc = _render(["open-webui"])
    assert "alertmanager" not in rc.compose_dict()["services"]
    assert not set(DELIVERY_KEYS) & set(rc.required_secrets)


# --------------------------------------------------------------------------- #
# The edge certificate, read by ops-controller for TlsCertExpiring.
# --------------------------------------------------------------------------- #


def test_with_the_edge_ops_controller_reads_the_certificate_caddy_serves():
    doc = _render(["edge", "monitoring"], site=EDGE_SITE).compose_dict()
    ops = doc["services"]["ops-controller"]
    assert f"{compose.EDGE_TLS_CERT_DIR}:{compose.EDGE_TLS_CERT_TARGET}:ro" in ops["volumes"]
    assert ops["environment"]["EDGE_TLS_CERT_FILE"] == f"{compose.EDGE_TLS_CERT_TARGET}/{compose.EDGE_TLS_CERT_FILE}"
    # The same host directory Caddy serves from, and the file its Caddyfile names.
    assert f"{compose.EDGE_TLS_CERT_DIR}:/etc/caddy/certs:ro" in doc["services"]["caddy"]["volumes"]
    caddyfile = (ROOT / "auth" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert f"/etc/caddy/certs/{compose.EDGE_TLS_CERT_FILE}" in caddyfile


def test_without_the_edge_ops_controller_mounts_no_certificate(monitored):
    _, doc = monitored
    ops = doc["services"]["ops-controller"]
    assert not any(compose.EDGE_TLS_CERT_TARGET in v for v in ops["volumes"])
    assert "EDGE_TLS_CERT_FILE" not in ops["environment"]


# --------------------------------------------------------------------------- #
# The tracked configs themselves.
# --------------------------------------------------------------------------- #


def _alerts():
    return [rule for group in RULES["groups"] for rule in group["rules"]]


def test_every_alert_has_a_known_severity_and_a_summary():
    for rule in _alerts():
        assert rule["labels"]["severity"] in {"critical", "warning", "none"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]


def test_the_incidents_this_was_built_for_each_have_an_alert():
    names = {rule["alert"] for rule in _alerts()}
    assert {"Watchdog", "DiskUsageHigh", "ContainerRestartLoop", "ContainerUnhealthy", "GpuLeasePastTtl",
            "GpuResidentEvictedWithoutLease", "LlamaCppDown", "TlsCertExpiring", "TargetDown",
            "GpuTemperatureHigh", "GpuPowerAboveLimit"} <= names


def test_watchdog_goes_only_to_the_heartbeat_and_everything_else_to_discord():
    route = ALERTMANAGER["route"]
    assert route["receiver"] == "discord"
    watchdog = [r for r in route["routes"] if r["matchers"] == ['alertname="Watchdog"']]
    assert len(watchdog) == 1 and watchdog[0]["receiver"] == "heartbeat"
    assert not watchdog[0].get("continue", False), "Watchdog would also post to Discord every minute"
    assert route["routes"][0] is watchdog[0], "the Watchdog route must match before any other"
    receivers = {r["name"] for r in ALERTMANAGER["receivers"]}
    assert {route["receiver"], *(r["receiver"] for r in route["routes"])} <= receivers


def test_no_delivery_url_is_written_into_the_public_config():
    """The URLs carry tokens: only their *_file paths may appear here."""
    for receiver in ALERTMANAGER["receivers"]:
        for kind, configs in receiver.items():
            if kind == "name":
                continue
            for config in configs:
                assert not {"url", "webhook_url"} & set(config), f"{receiver['name']}: a URL value in the file"
    assert "https://" not in ALERTMANAGER_TEXT and "http://" not in ALERTMANAGER_TEXT
