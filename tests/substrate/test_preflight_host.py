"""Host preflight: the machine can actually run the rendered stack, checked before `ordo up` starts it.

Each failing check is one actionable line. The host facts (docker, ports, disk) are gathered by
I/O helpers and passed in, so every verdict here is tested without docker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo import cli, preflight
from ordo.preflight import HostFacts

READY = HostFacts(docker_error=None, compose_version="2.39.1", runtimes=frozenset({"runc", "nvidia"}),
                  busy_ports=frozenset(), disk_path="/var/lib/docker", disk_free_gb=500.0)

NVIDIA = {"deploy": {"resources": {"reservations": {"devices": [
    {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}]}}}}

SERVICES = {
    "llamacpp": {"image": "x", **NVIDIA, "environment": {"A": "1"}},
    "model-gateway": {"image": "x", "environment": {"LITELLM_MASTER_KEY": "${LITELLM_MASTER_KEY}"}},
    "comfyui": {"image": "x", "environment": {"HF_TOKEN": "${HF_TOKEN:-}"}},
    "open-webui": {"image": "x", "ports": ["127.0.0.1:8443:8080"]},
    "caddy": {"image": "x", "ports": ["${CADDY_BIND:?CADDY_BIND must be set (non-empty)}:443:443"]},
}
ENV = {"CADDY_BIND": "0.0.0.0"}
SECRET_KEYS = ["LITELLM_MASTER_KEY", "HF_TOKEN"]
OPTIONAL = ["HF_TOKEN"]


def _checks(tmp_path, facts=READY, services=SERVICES, secrets="LITELLM_MASTER_KEY=sk-1\nHF_TOKEN=\n",
            model_gb=22.0):
    path = tmp_path / "secrets.env"
    if secrets is not None:
        path.write_text(secrets, encoding="utf-8")
    return {c.name: c for c in preflight.host_checks(
        services, ENV, facts, secret_keys=SECRET_KEYS, optional_secrets=OPTIONAL,
        secrets_path=str(path), model_gb=model_gb)}


def _failed(checks):
    return [c for c in checks.values() if c.blocking and not c.ok]


def test_a_ready_host_passes_every_blocking_check(tmp_path):
    assert _failed(_checks(tmp_path)) == []


def test_published_ports_resolve_the_bind_variable():
    assert preflight.published_ports(SERVICES, ENV) == [("127.0.0.1", 8443), ("0.0.0.0", 443)]


def test_unreachable_docker_blocks_with_one_line(tmp_path):
    facts = HostFacts(**{**READY.__dict__, "docker_error": "Cannot connect to the Docker daemon"})
    checks = _checks(tmp_path, facts=facts)
    failed = _failed(checks)
    assert [c.name for c in failed] == ["docker daemon reachable"]
    assert "start Docker" in failed[0].detail and "\n" not in failed[0].detail


@pytest.mark.parametrize("version", [None, "1.29.2"])
def test_compose_v2_is_required(tmp_path, version):
    facts = HostFacts(**{**READY.__dict__, "compose_version": version})
    check = _checks(tmp_path, facts=facts)["docker compose v2 present"]
    assert check.blocking and not check.ok


def test_nvidia_runtime_is_required_when_the_render_reserves_a_gpu(tmp_path):
    facts = HostFacts(**{**READY.__dict__, "runtimes": frozenset({"runc"})})
    check = _checks(tmp_path, facts=facts)["NVIDIA container runtime present"]
    assert check.blocking and not check.ok
    assert "llamacpp" in check.detail


def test_nvidia_runtime_is_not_needed_without_a_gpu_reservation(tmp_path):
    facts = HostFacts(**{**READY.__dict__, "runtimes": frozenset({"runc"})})
    cpu_only = {name: {k: v for k, v in svc.items() if k != "deploy"} for name, svc in SERVICES.items()}
    assert _failed(_checks(tmp_path, facts=facts, services=cpu_only)) == []


def test_too_little_disk_for_the_model_blocks(tmp_path):
    facts = HostFacts(**{**READY.__dict__, "disk_free_gb": 10.0})
    check = _checks(tmp_path, facts=facts, model_gb=22.0)["free disk for the model"]
    assert check.blocking and not check.ok
    assert "22" in check.detail and "10" in check.detail


def test_a_published_port_held_by_another_process_blocks(tmp_path):
    facts = HostFacts(**{**READY.__dict__, "busy_ports": frozenset({("127.0.0.1", 8443)})})
    check = _checks(tmp_path, facts=facts)["host ports free"]
    assert check.blocking and not check.ok
    assert "127.0.0.1:8443" in check.detail


def test_a_blank_required_secret_blocks_and_names_only_the_key(tmp_path):
    checks = _checks(tmp_path, secrets="LITELLM_MASTER_KEY=\nHF_TOKEN=\n")
    required = checks["required secrets set"]
    assert required.blocking and not required.ok
    assert "LITELLM_MASTER_KEY" in required.detail and "HF_TOKEN" not in required.detail


def test_a_blank_optional_secret_does_not_block(tmp_path):
    checks = _checks(tmp_path)
    assert checks["required secrets set"].ok
    optional = checks["optional secrets"]
    assert not optional.blocking and "HF_TOKEN" in optional.detail


def test_secrets_are_scoped_to_the_services_that_start(tmp_path):
    only_webui = {"open-webui": SERVICES["open-webui"]}
    assert _checks(tmp_path, services=only_webui, secrets="")["required secrets set"].ok


def test_a_missing_secrets_file_blocks(tmp_path):
    check = _checks(tmp_path, secrets=None)["required secrets set"]
    assert check.blocking and not check.ok and "ordo init" in check.detail


def test_secret_values_never_reach_the_output(tmp_path):
    checks = _checks(tmp_path, secrets="LITELLM_MASTER_KEY=sk-very-secret\nHF_TOKEN=\n")
    assert all("sk-very-secret" not in c.detail for c in checks.values())


def test_project_held_ports_parse_ranges():
    held = preflight.parse_docker_ports("0.0.0.0:443->443/tcp, 0.0.0.0:8443-8445->8443-8445/tcp, 9000/tcp")
    assert held == {443, 8443, 8444, 8445}


# --- `ordo up` runs it first ---


def _rendered(out: Path) -> None:
    import json

    import yaml
    out.mkdir(parents=True, exist_ok=True)
    (out / "docker-compose.yml").write_text(yaml.safe_dump({"services": SERVICES}), encoding="utf-8")
    (out / ".env").write_text("CADDY_BIND=0.0.0.0\n", encoding="utf-8")
    (out / "secrets.env").write_text("LITELLM_MASTER_KEY=\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "model": {"id": "m", "vram_gb": 1.0}, "required_secrets": SECRET_KEYS,
        "optional_secrets": OPTIONAL}), encoding="utf-8")


def test_up_refuses_before_compose_when_preflight_fails(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    _rendered(out)
    monkeypatch.setattr(preflight, "gather_host_facts", lambda *a, **k: READY)
    ran = []
    monkeypatch.setattr("ordo.bringup.bring_up", lambda *a, **k: ran.append(a) or 0)
    assert cli.main(["up", "--all", "--out", str(out)]) == 1
    assert ran == []
    printed = capsys.readouterr().out
    assert "LITELLM_MASTER_KEY" in printed and "--no-preflight" in printed


def test_up_proceeds_when_preflight_passes(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _rendered(out)
    (out / "secrets.env").write_text("LITELLM_MASTER_KEY=sk-1\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "gather_host_facts", lambda *a, **k: READY)
    ran = []
    monkeypatch.setattr("ordo.bringup.bring_up", lambda *a, **k: ran.append(a) or 0)
    assert cli.main(["up", "--all", "--out", str(out)]) == 0
    assert ran


def test_up_no_preflight_skips_it(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _rendered(out)

    def boom(*a, **k):
        raise AssertionError("preflight ran")

    monkeypatch.setattr(preflight, "gather_host_facts", boom)
    monkeypatch.setattr("ordo.bringup.bring_up", lambda *a, **k: 0)
    assert cli.main(["up", "--all", "--no-preflight", "--out", str(out)]) == 0


# --- which secrets a render can run without is declared in the manifests ---


def test_optional_secrets_are_declared_by_the_plugins_that_read_them():
    from ordo.catalog import Catalog
    from ordo.config import Source
    from ordo.plugins import PluginRegistry
    from ordo.render import render

    root = Path(__file__).resolve().parents[2]
    source = Source.from_dict({"hardware": {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128},
                               "model": "auto", "plugins": "auto"})
    rc = render(source, Catalog.load(root / "catalog" / "models.yaml"), PluginRegistry.load(root / "services"))
    assert {"HF_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"} <= set(rc.optional_secrets)
    assert "LITELLM_MASTER_KEY" not in rc.optional_secrets
    manifest = rc.manifest()
    assert manifest["required_secrets"] == rc.required_secrets
    assert manifest["optional_secrets"] == rc.optional_secrets
    assert manifest["model"]["disk_gb"] > 0


def test_an_optional_secret_must_be_one_the_plugin_declares():
    from ordo.plugins import Plugin
    with pytest.raises(ValueError):
        Plugin.from_dict({"id": "p", "secrets": ["A"], "optional_secrets": ["B"]})
