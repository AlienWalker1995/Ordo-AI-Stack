"""Bind-mounted config files: a content edit changes the service's compose config hash.

Compose hashes a service's definition, not the files it bind-mounts, so an edited Prometheus rule
(#311) or ClickHouse config (#304) used to leave `ordo apply` reporting "no change" while the running
service kept the old file. The render now labels each read-only config bind from the checkout with
its content digest (ordo/render/bind_configs.py), so the label, the config hash and the changed set
all move with the content. The host and ops-controller compute it with the same function.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ordo.render import bind_configs
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
HW = {"gpus": [{"vram_gb": 32}], "ram_gb": 128}
BASE = "${BASE_PATH:?BASE_PATH must be set (non-empty)}"
DATA = "${DATA_PATH:?DATA_PATH must be set (non-empty)}"
LABEL = bind_configs.LABEL_PREFIX


@pytest.fixture(autouse=True)
def _no_checkout_override(monkeypatch):
    """The host's case: the checkout is read at BASE_PATH (ops-controller sets the override)."""
    monkeypatch.delenv(bind_configs.CHECKOUT_DIR_ENV, raising=False)


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / "monitoring" / "rules").mkdir(parents=True)
    (checkout / "monitoring" / "prometheus.yml").write_text("scrape_interval: 15s\n", encoding="utf-8")
    (checkout / "monitoring" / "rules" / "a.yml").write_text("groups: []\n", encoding="utf-8")
    return checkout


def _labels(volumes: list, checkout: Path, **env: str) -> dict[str, str]:
    services = {"svc": {"image": "x", "volumes": list(volumes)}}
    bind_configs.add_labels(services, {"BASE_PATH": checkout.as_posix(), **env})
    return services["svc"].get("labels", {})


# --------------------------------------------------------------------------- #
# what is digested
# --------------------------------------------------------------------------- #


def test_a_read_only_checkout_file_is_labelled_with_its_content_digest(tmp_path):
    checkout = _checkout(tmp_path)
    bind = f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
    before = _labels([bind], checkout)
    assert set(before) == {f"{LABEL}/etc/prometheus/prometheus.yml"}

    (checkout / "monitoring" / "prometheus.yml").write_text("scrape_interval: 30s\n", encoding="utf-8")
    after = _labels([bind], checkout)
    assert after.keys() == before.keys()
    assert after != before, "an edited file must change the label, or apply cannot see the edit"


def test_the_digest_is_of_the_bytes_the_container_reads(tmp_path):
    """No line-ending normalisation: the container sees the bytes on disk, CRLF included."""
    checkout = _checkout(tmp_path)
    bind = f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
    (checkout / "monitoring" / "prometheus.yml").write_bytes(b"scrape_interval: 15s\n")
    before = _labels([bind], checkout)
    (checkout / "monitoring" / "prometheus.yml").write_bytes(b"scrape_interval: 15s\r\n")
    assert _labels([bind], checkout) != before


def test_a_small_directory_is_digested_by_name_and_content(tmp_path):
    checkout = _checkout(tmp_path)
    bind = f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"
    first = _labels([bind], checkout)
    assert set(first) == {f"{LABEL}/etc/prometheus/rules"}

    (checkout / "monitoring" / "rules" / "b.yml").write_text("groups: []\n", encoding="utf-8")
    added = _labels([bind], checkout)
    assert added != first, "a new rule file must change the label"

    (checkout / "monitoring" / "rules" / "b.yml").rename(checkout / "monitoring" / "rules" / "c.yml")
    assert _labels([bind], checkout) != added, "a renamed file must change the label"


def test_a_directory_over_the_size_limit_is_left_out(tmp_path, monkeypatch):
    """A large directory is watched data (docs a RAG ingester reads, say), not startup config."""
    checkout = _checkout(tmp_path)
    monkeypatch.setattr(bind_configs, "MAX_DIR_FILES", 1)
    assert _labels([f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"], checkout)
    (checkout / "monitoring" / "rules" / "b.yml").write_text("x\n", encoding="utf-8")
    assert _labels([f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"], checkout) == {}

    monkeypatch.setattr(bind_configs, "MAX_DIR_FILES", 100)
    monkeypatch.setattr(bind_configs, "MAX_DIR_BYTES", 5)
    assert _labels([f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"], checkout) == {}


def test_a_missing_source_is_labelled_absent_and_its_arrival_changes_the_label(tmp_path):
    checkout = _checkout(tmp_path)
    bind = f"{BASE}/monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro"
    assert _labels([bind], checkout) == {f"{LABEL}/etc/alertmanager/alertmanager.yml": bind_configs.ABSENT}
    (checkout / "monitoring" / "alertmanager.yml").write_text("route: {}\n", encoding="utf-8")
    assert _labels([bind], checkout)[f"{LABEL}/etc/alertmanager/alertmanager.yml"] != bind_configs.ABSENT


def test_the_long_bind_syntax_is_read_the_same_way(tmp_path):
    checkout = _checkout(tmp_path)
    short = _labels([f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"], checkout)
    long = _labels([{"type": "bind", "source": f"{BASE}/monitoring/prometheus.yml",
                     "target": "/etc/prometheus/prometheus.yml", "read_only": True}], checkout)
    assert long == short


# --------------------------------------------------------------------------- #
# what is not: data, models, the render's own outputs, writable binds
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("volume", [
    f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml",       # writable: state
    f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:rw",
    {"type": "bind", "source": f"{BASE}/monitoring/rules", "target": "/rules"},
    f"{DATA}/dashboard:/data/dashboard:ro",                                  # DATA_PATH
    f"{BASE}/data/thing:/thing:ro",                                          # runtime state dirs
    f"{BASE}/models/gguf:/models:ro",
    f"{BASE}/out/secrets/token:/run/secrets/token:ro",                       # materialize labels it
    f"{BASE}/out/model-gateway:/config:ro",                                  # the render labels it
    f"{BASE}:/checkout:ro",                                                  # the checkout itself
    "${CODE_ROOT:-/c/dev}:/c/dev:ro",                                        # not the checkout
    "/var/run/docker.sock:/var/run/docker.sock",
    "prometheus-data:/prometheus",                                           # a named volume
])
def test_data_state_and_render_outputs_are_never_digested(tmp_path, volume):
    assert _labels([volume], _checkout(tmp_path)) == {}


def test_a_path_under_data_path_is_left_out_wherever_data_path_lives(tmp_path):
    """DATA_PATH is a site value: a checkout path under it is data even when it is not `data/`."""
    checkout = _checkout(tmp_path)
    state = checkout / "monitoring" / "rules"
    bind = f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"
    assert _labels([bind], checkout)
    assert _labels([bind], checkout, DATA_PATH=state.as_posix()) == {}
    assert _labels([bind], checkout, DATA_PATH=f"{state.as_posix()}/") == {}


# --------------------------------------------------------------------------- #
# where the checkout is read, and failing closed
# --------------------------------------------------------------------------- #


def test_no_base_path_means_no_labels():
    """Without BASE_PATH every `${BASE_PATH:?}` bind fails compose, so nothing can run to drift."""
    services = {"svc": {"image": "x", "volumes": [f"{BASE}/monitoring/prometheus.yml:/p.yml:ro"]}}
    bind_configs.add_labels(services, {})
    assert "labels" not in services["svc"]


def test_a_host_base_path_that_does_not_exist_labels_each_bind_absent(tmp_path):
    """What the container would mount: nothing. A render for another host (a fixture) still works."""
    services = {"svc": {"image": "x", "volumes": [f"{BASE}/monitoring/prometheus.yml:/p.yml:ro"]}}
    bind_configs.add_labels(services, {"BASE_PATH": (tmp_path / "nowhere").as_posix()})
    assert services["svc"]["labels"] == {f"{LABEL}/p.yml": bind_configs.ABSENT}


def test_ops_controller_refuses_to_render_without_its_checkout_mount(tmp_path, monkeypatch):
    """A missing mount would label every bind absent and recreate them all: refuse instead."""
    monkeypatch.setenv(bind_configs.CHECKOUT_DIR_ENV, (tmp_path / "not-mounted").as_posix())
    services = {"svc": {"image": "x", "volumes": [f"{BASE}/monitoring/prometheus.yml:/p.yml:ro"]}}
    with pytest.raises(ValueError, match="cannot digest the bind-mounted config"):
        bind_configs.add_labels(services, {"BASE_PATH": _checkout(tmp_path).as_posix()})


def test_ops_controller_reads_the_checkout_at_its_mount_and_agrees_with_the_host(tmp_path, monkeypatch):
    """Inside ops-controller BASE_PATH names a host path it cannot open; it reads the same files at
    its read-only /checkout mount (ORDO_CHECKOUT_DIR), and so labels exactly what the host does."""
    checkout = _checkout(tmp_path)
    bind = f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
    host = _labels([bind], checkout)
    monkeypatch.setenv(bind_configs.CHECKOUT_DIR_ENV, checkout.as_posix())
    control_plane = _labels([bind], tmp_path / "host-only-path")
    assert control_plane == host


# --------------------------------------------------------------------------- #
# the render: every service, and ops-controller's mount
# --------------------------------------------------------------------------- #


def _render_doc(base: Path) -> dict:
    site = {"BASE_PATH": base.as_posix(), "DATA_PATH": (base / "data").as_posix()}
    rendered = render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": ["monitoring"],
                                        "site": site}), CATALOG)
    return rendered.compose_dict()


def _bind_labels(doc: dict) -> dict[str, dict[str, str]]:
    return {name: {k: v for k, v in (spec.get("labels") or {}).items() if k.startswith(LABEL)}
            for name, spec in doc["services"].items()}


def _repo_copy(tmp_path: Path) -> Path:
    """A checkout holding the tracked config the monitoring services bind."""
    base = tmp_path / "base"
    shutil.copytree(ROOT / "monitoring", base / "monitoring")
    shutil.copytree(ROOT / "scripts" / "llamacpp", base / "scripts" / "llamacpp")
    return base


def test_the_render_labels_each_service_s_config_binds(tmp_path):
    doc = _render_doc(_repo_copy(tmp_path))
    labels = _bind_labels(doc)
    assert f"{LABEL}/etc/prometheus/prometheus.yml" in labels["prometheus"]
    assert f"{LABEL}/etc/prometheus/rules" in labels["prometheus"]
    assert f"{LABEL}/etc/alertmanager/alertmanager.yml" in labels["alertmanager"]
    # A DATA_PATH bind (the dashboard's state) is never digested.
    assert labels["dashboard"] == {}


def test_editing_one_bound_file_changes_only_its_services_labels(tmp_path):
    base = _repo_copy(tmp_path)
    before = _bind_labels(_render_doc(base))
    rules = sorted((base / "monitoring" / "prometheus" / "rules").iterdir())[0]
    rules.write_text(rules.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    after = _bind_labels(_render_doc(base))
    changed = sorted(name for name in before if before[name] != after[name])
    assert changed == ["prometheus"]


def test_ops_controller_mounts_the_checkout_read_only_and_reads_it_there(tmp_path):
    ops = _render_doc(_repo_copy(tmp_path))["services"]["ops-controller"]
    assert f"{BASE}:{bind_configs.OPS_CONTROLLER_CHECKOUT_DIR}:ro" in ops["volumes"]
    assert ops["environment"][bind_configs.CHECKOUT_DIR_ENV] == bind_configs.OPS_CONTROLLER_CHECKOUT_DIR


# --------------------------------------------------------------------------- #
# the real compose: the label moves the config hash apply compares
# --------------------------------------------------------------------------- #


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.docker
def test_compose_config_hash_follows_the_bound_file(tmp_path):
    if not _docker_available():
        pytest.skip("docker compose is not available")
    checkout = _checkout(tmp_path)
    compose_file = tmp_path / "docker-compose.yml"

    def config_hash() -> str:
        services = {"svc": {"image": "busybox:1.37.0", "volumes": [
            f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]}}
        bind_configs.add_labels(services, {"BASE_PATH": checkout.as_posix()})
        compose_file.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")
        proc = subprocess.run(["docker", "compose", "-p", "ordo-bindcfg-test", "-f", str(compose_file),
                               "config", "--hash", "svc"], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "BASE_PATH": checkout.as_posix()})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.split()[1]

    before = config_hash()
    assert config_hash() == before, "an unchanged file must leave the hash alone"
    (checkout / "monitoring" / "prometheus.yml").write_text("scrape_interval: 30s\n", encoding="utf-8")
    assert config_hash() != before
