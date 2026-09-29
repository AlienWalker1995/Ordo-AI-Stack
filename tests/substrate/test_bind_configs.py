"""Declared config binds: a content edit changes the service's compose config hash.

Compose hashes a service's definition, not the files it bind-mounts, so an edited Prometheus rule
(#311) or ClickHouse config (#304) used to leave `ordo apply` reporting "no change" while the running
service kept the old file. A service now declares its config binds (`config_mounts:`); each renders
as an `ordo.bind-config.<path>` label interpolated from out/bind-configs.env, which only the host
writes (ordo/render/bind_configs.py). A control-plane render keeps the host's values, so a dashboard
model switch never deploys an unapplied checkout edit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ordo.control.apply import RenderApply
from ordo.control.source import StackSource
from ordo.render import bind_configs, stack
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HW = {"gpus": [{"vram_gb": 32}], "ram_gb": 128}
BASE = "${BASE_PATH:?BASE_PATH must be set (non-empty)}"
DATA = "${DATA_PATH:?DATA_PATH must be set (non-empty)}"
LABEL = bind_configs.LABEL_PREFIX
PROM_VAR = bind_configs.digest_var("prometheus", "/etc/prometheus/rules")


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #


def test_a_declared_bind_renders_a_label_interpolated_from_the_values_file():
    spec = {"volumes": [f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"]}
    bind_configs.add_labels("prometheus", spec, ["/etc/prometheus/rules"])
    assert spec["labels"] == {f"{LABEL}/etc/prometheus/rules": f"${{{PROM_VAR}:-}}"}
    assert PROM_VAR == "ORDO_BIND_CONFIG_SHA256_PROMETHEUS_ETC_PROMETHEUS_RULES"


def test_an_undeclared_bind_is_never_labelled():
    spec = {"volumes": [f"{BASE}/docs:/watch/stack-docs:ro"]}
    bind_configs.add_labels("rag-ingestion", spec, [])
    assert "labels" not in spec


@pytest.mark.parametrize("volume, match", [
    (f"{BASE}/monitoring/rules:/etc/prometheus/rules", "not a read-only bind"),        # writable
    (f"{DATA}/rules:/etc/prometheus/rules:ro", "not a read-only bind"),               # DATA_PATH
    ("prometheus-rules:/etc/prometheus/rules:ro", "not a read-only bind"),            # named volume
    (f"{BASE}/data/rules:/etc/prometheus/rules:ro", "runtime state or a render output"),
    (f"{BASE}/models/rules:/etc/prometheus/rules:ro", "runtime state or a render output"),
    (f"{BASE}/out/rules:/etc/prometheus/rules:ro", "runtime state or a render output"),
])
def test_only_a_read_only_checkout_config_bind_can_be_declared(volume, match):
    with pytest.raises(ValueError, match=match):
        bind_configs.parse_config_mounts("svc", ["/etc/prometheus/rules"], [volume])


def test_a_declaration_naming_no_bind_or_listed_twice_is_refused():
    volumes = [f"{BASE}/monitoring/rules:/etc/prometheus/rules:ro"]
    with pytest.raises(ValueError, match="not a read-only bind"):
        bind_configs.parse_config_mounts("svc", ["/etc/elsewhere"], volumes)
    with pytest.raises(ValueError, match="more than once"):
        bind_configs.parse_config_mounts("svc", ["/etc/prometheus/rules"] * 2, volumes)


def test_the_manifests_declare_every_checkout_bind_or_it_is_a_reviewed_exception():
    """Declaring is explicit, so a new read-only checkout bind has to be decided: config (declare it
    under config_mounts) or read live (add it here, with the reason)."""
    read_live = {
        ("rag-ingestion", "/watch/stack-docs"): "the ingester watches the docs and re-reads them",
        ("evals", "/app"): "a one-shot job: every `run --rm` starts a fresh container",
        ("ltx-trainer", "/ordo/lease-exec.py"): "the idle container runs it anew on every `docker exec`",
    }
    undeclared = set()
    for plugin in REGISTRY.plugins:
        for service in plugin.services:
            for target, path in bind_configs.checkout_binds(service.volumes).items():
                if path.split("/")[0] != "out" and target not in service.config_mounts:
                    undeclared.add((service.name, target))
    assert undeclared == set(read_live), sorted(undeclared ^ set(read_live))


# --------------------------------------------------------------------------- #
# the digest
# --------------------------------------------------------------------------- #


def test_a_file_digest_is_of_the_bytes_the_container_reads(tmp_path):
    file = tmp_path / "prometheus.yml"
    file.write_bytes(b"scrape_interval: 15s\n")
    before = bind_configs.digest_path(file)
    file.write_bytes(b"scrape_interval: 15s\r\n")
    assert bind_configs.digest_path(file) != before
    assert bind_configs.digest_path(tmp_path / "missing.yml") == bind_configs.ABSENT


def test_a_directory_digest_follows_names_and_content_and_skips_python_caches(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.yml").write_bytes(b"groups: []\n")
    first = bind_configs.digest_path(rules)

    (rules / "__pycache__").mkdir()
    (rules / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (rules / "y.pyc").write_bytes(b"\x00")
    assert bind_configs.digest_path(rules) == first, "a test run's caches must not recreate anything"

    (rules / "b.yml").write_bytes(b"groups: []\n")
    added = bind_configs.digest_path(rules)
    assert added != first
    (rules / "b.yml").rename(rules / "c.yml")
    assert bind_configs.digest_path(rules) != added


# --------------------------------------------------------------------------- #
# the values file: written by the host, kept by the control plane
# --------------------------------------------------------------------------- #


def _checkout(tmp_path: Path) -> Path:
    """A checkout holding the tracked config the monitoring services and llamacpp bind."""
    base = tmp_path / "base"
    shutil.copytree(ROOT / "monitoring", base / "monitoring")
    shutil.copytree(ROOT / "scripts" / "llamacpp", base / "scripts" / "llamacpp")
    return base


def _source(base: Path) -> dict:
    return {"hardware": HW, "model": "auto", "plugins": ["monitoring"],
            "site": {"BASE_PATH": base.as_posix(), "DATA_PATH": (base / "data").as_posix()}}


def _values(out: Path) -> dict[str, str]:
    text = (out / bind_configs.VALUES_ENV_FILE).read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if line)


def test_the_host_render_writes_each_declared_bind_s_digest(tmp_path):
    base = _checkout(tmp_path)
    out = tmp_path / "out"
    render(Source.from_dict(_source(base)), CATALOG).write(out, refresh_bind_configs=True)
    doc = yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))
    assert doc["services"]["prometheus"]["labels"][f"{LABEL}/etc/prometheus/rules"] == f"${{{PROM_VAR}:-}}"
    values = _values(out)
    assert values[PROM_VAR] == bind_configs.digest_path(base / "monitoring" / "prometheus" / "rules")
    assert values[bind_configs.digest_var("llamacpp", "/llamacpp-scripts")] != bind_configs.ABSENT
    assert "dashboard" not in {bind.service for bind in bind_configs.config_binds(doc)}


def test_editing_one_bound_file_changes_only_its_own_digest(tmp_path):
    base = _checkout(tmp_path)
    out = tmp_path / "out"
    rendered = render(Source.from_dict(_source(base)), CATALOG)
    rendered.write(out, refresh_bind_configs=True)
    before = _values(out)
    rule = sorted((base / "monitoring" / "prometheus" / "rules").iterdir())[0]
    rule.write_bytes(rule.read_bytes() + b"\n# edited\n")
    rendered.write(out, refresh_bind_configs=True)
    after = _values(out)
    assert sorted(var for var in before if before[var] != after[var]) == [PROM_VAR]


def test_without_base_path_the_values_file_is_empty(tmp_path):
    render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": ["monitoring"]}),
           CATALOG).write(tmp_path, refresh_bind_configs=True)
    assert _values(tmp_path) == {}


def test_a_control_plane_render_keeps_the_host_s_digests(tmp_path):
    """A dashboard model switch re-renders out/ inside ops-controller (RenderApply.commit). It must
    not pick up a checkout edit the operator has not applied: the values stay the host's."""
    base = _checkout(tmp_path)
    out = tmp_path / "out"
    source_path = out / "ordo.yaml"
    out.mkdir()
    source_path.write_text(yaml.safe_dump(_source(base)), encoding="utf-8")
    render(Source.load(source_path), CATALOG).write(out, refresh_bind_configs=True)
    deployed = _values(out)

    rule = sorted((base / "monitoring" / "prometheus" / "rules").iterdir())[0]
    rule.write_bytes(rule.read_bytes() + b"\n# half-edited, not applied\n")
    source = StackSource(source_path, CATALOG, REGISTRY, out, substrate_digest="")
    text = source_path.read_text(encoding="utf-8")
    applied, failure = RenderApply(source, None, None, None).commit(text, source.render())
    assert (applied, failure) == (None, None)
    assert _values(out) == deployed


def test_a_render_makes_sure_the_values_file_exists(tmp_path):
    render(Source.from_dict({"hardware": HW, "model": "auto"}), CATALOG).write(tmp_path)
    assert (tmp_path / bind_configs.VALUES_ENV_FILE).read_text(encoding="utf-8") == ""


def test_every_compose_call_loads_the_values_file():
    argv = stack.compose_argv("/d", "ordo", "up")
    assert argv[argv.index("/d/secret-files.env") + 1:argv.index("up")] == \
        ["--env-file", f"/d/{bind_configs.VALUES_ENV_FILE}"]


# --------------------------------------------------------------------------- #
# the real compose: the value moves the config hash apply compares
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
    checkout = tmp_path / "checkout"
    (checkout / "monitoring").mkdir(parents=True)
    config = checkout / "monitoring" / "prometheus.yml"
    config.write_text("scrape_interval: 15s\n", encoding="utf-8")
    spec = {"image": "busybox:1.37.0",
            "volumes": [f"{BASE}/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro"]}
    bind_configs.add_labels("svc", spec, ["/etc/prometheus/prometheus.yml"])
    doc = {"services": {"svc": spec}}
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")

    def config_hash() -> str:
        bind_configs.write_values(doc, {"BASE_PATH": checkout.as_posix()}, tmp_path)
        proc = subprocess.run(["docker", "compose", "-p", "ordo-bindcfg-test", "-f",
                               str(tmp_path / "docker-compose.yml"), "--env-file",
                               str(tmp_path / bind_configs.VALUES_ENV_FILE), "config", "--hash", "svc"],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "BASE_PATH": checkout.as_posix()})
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.split()[1]

    before = config_hash()
    assert config_hash() == before, "an unchanged file must leave the hash alone"
    config.write_text("scrape_interval: 30s\n", encoding="utf-8")
    assert config_hash() != before
