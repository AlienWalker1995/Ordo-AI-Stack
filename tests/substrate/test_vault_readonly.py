"""The vault's read-only folders (site: MEMORY_VAULT_READONLY_DIRS): a nested `:ro` bind over the
read-write vault bind of the memory-vault MCP server and of the agent, so the kernel refuses a write."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from ordo.host import preflight
from ordo.render import vault_readonly
from ordo.render.agents import AgentRegistry
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
AGENTS = AgentRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
VAULT_SOURCE = "${MEMORY_VAULT_PATH:?MEMORY_VAULT_PATH must be set in ordo.yaml site}"
KEY = "MEMORY_VAULT_READONLY_DIRS"
# The vault sits below the code root (the agent's `${CODE_ROOT:-/c/dev}:/c/dev` bind) and not below DATA_PATH
# (its `${DATA_PATH}:/workspace/data` bind).
BASE_SITE = {"MEMORY_VAULT_PATH": "/srv/dev/vault", "CODE_ROOT": "/srv/dev", "DATA_PATH": "/srv/data"}


def _render(site_extra=None, site=None):
    site = {**(BASE_SITE if site is None else site), **(site_extra or {})}
    source = Source.from_dict({"hardware": P_5090, "plugins": ["memory-vault"], "site": site})
    return render(source, CATALOG, REGISTRY, agents=AGENTS)


def _compose(site_extra=None, site=None):
    return _render(site_extra, site).compose_dict()["services"]


def test_unset_adds_no_mount_and_no_warning():
    services = _compose()
    assert services["mcp-memory-vault"]["volumes"] == [f"{VAULT_SOURCE}:/vault"]
    assert not any(v.endswith(":ro") and "MEMORY_VAULT_PATH" in v for v in services["agent"]["volumes"])
    assert not any(KEY in w for w in _render().warnings)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_is_the_same_as_unset(blank):
    assert _compose({KEY: blank}) == _compose()


def test_one_folder_is_mounted_read_only_in_the_mcp_server_and_the_agent():
    services = _compose({KEY: "notes/Canonical"})
    assert services["mcp-memory-vault"]["volumes"] == [
        f"{VAULT_SOURCE}:/vault",
        f"{VAULT_SOURCE}/notes/Canonical:/vault/notes/Canonical:ro",
    ]
    # the mirror mount is `${CODE_ROOT:-/c/dev}:/c/dev`, and the vault is at <code root>/vault
    assert f"{VAULT_SOURCE}/notes/Canonical:/c/dev/vault/notes/Canonical:ro" in services["agent"]["volumes"]


def test_two_folders_each_get_a_mount_in_order():
    services = _compose({KEY: "notes/Canonical, plans/Approved/"})
    assert services["mcp-memory-vault"]["volumes"][1:] == [
        f"{VAULT_SOURCE}/notes/Canonical:/vault/notes/Canonical:ro",
        f"{VAULT_SOURCE}/plans/Approved:/vault/plans/Approved:ro",
    ]
    agent_ro = [v for v in services["agent"]["volumes"] if v.startswith(VAULT_SOURCE)]
    assert agent_ro == [
        f"{VAULT_SOURCE}/notes/Canonical:/c/dev/vault/notes/Canonical:ro",
        f"{VAULT_SOURCE}/plans/Approved:/c/dev/vault/plans/Approved:ro",
    ]


def test_a_repeated_folder_is_mounted_once():
    services = _compose({KEY: "notes,notes/"})
    assert len([v for v in services["mcp-memory-vault"]["volumes"] if v.endswith(":ro")]) == 1


def test_the_read_write_vault_bind_comes_first_so_the_nested_bind_lands_on_top():
    volumes = _compose({KEY: "notes"})["mcp-memory-vault"]["volumes"]
    assert volumes[0] == f"{VAULT_SOURCE}:/vault"


@pytest.mark.parametrize("entry, reason", [
    ("/etc", "absolute"),
    ("notes/../..", "'..'"),
    ("..", "'..'"),
    ("notes/./x", "'.'"),
    ("notes//x", "empty path segment"),
    ("notes,,x", "is empty"),
    ("notes\\Canonical", "backslash"),
    ("C:/notes", "colon"),
    ("notes/$HOME", "dollar"),
])
def test_unsafe_entries_are_refused(entry, reason):
    with pytest.raises(ValueError) as refused:
        _render({KEY: entry})
    assert KEY in str(refused.value) and reason in str(refused.value)


def test_a_folder_list_without_a_vault_path_is_refused():
    with pytest.raises(ValueError, match="MEMORY_VAULT_PATH"):
        _render({KEY: "notes"}, site={})


def test_a_vault_under_data_path_is_mounted_through_the_data_bind():
    site = {"MEMORY_VAULT_PATH": "/srv/data/memory-vault", "CODE_ROOT": "/srv/dev", "DATA_PATH": "/srv/data"}
    agent_ro = [v for v in _compose({KEY: "notes"}, site=site)["agent"]["volumes"] if v.startswith(VAULT_SOURCE)]
    assert agent_ro == [f"{VAULT_SOURCE}/notes:/workspace/data/memory-vault/notes:ro"]


def test_a_vault_under_both_binds_is_mounted_through_both():
    site = {"MEMORY_VAULT_PATH": "/srv/dev/data/vault", "CODE_ROOT": "/srv/dev", "DATA_PATH": "/srv/dev/data"}
    agent_ro = [v for v in _compose({KEY: "notes"}, site=site)["agent"]["volumes"] if v.startswith(VAULT_SOURCE)]
    assert agent_ro == [f"{VAULT_SOURCE}/notes:/workspace/data/vault/notes:ro",
                        f"{VAULT_SOURCE}/notes:/c/dev/data/vault/notes:ro"]


def test_a_vault_under_neither_bind_skips_the_agent_mount_with_a_warning():
    rc = _render({KEY: "notes", "CODE_ROOT": "/srv/other"})
    services = rc.compose_dict()["services"]
    assert f"{VAULT_SOURCE}/notes:/vault/notes:ro" in services["mcp-memory-vault"]["volumes"]
    assert not any(v.startswith(f"{VAULT_SOURCE}/notes:") for v in services["agent"]["volumes"])
    assert any(KEY in w and "no read-write bind of the agent" in w for w in rc.warnings)


def test_a_read_only_bind_of_the_vault_is_not_a_way_in():
    mounts, warnings = vault_readonly.agent_mounts(
        ["notes"], "/srv/vault", ["/srv/vault:/seen:ro", "named-volume:/data"], {})
    assert mounts == [] and warnings


def test_the_code_root_defaults_to_the_mirror_default():
    services = _compose({KEY: "notes"}, site={"MEMORY_VAULT_PATH": "/c/dev/vault", "DATA_PATH": "/srv/data"})
    assert f"{VAULT_SOURCE}/notes:/c/dev/vault/notes:ro" in services["agent"]["volumes"]


def test_a_windows_spelling_of_the_code_root_matches_the_mirror_spelling():
    services = _compose({KEY: "notes", "CODE_ROOT": r"C:\dev", "MEMORY_VAULT_PATH": "/c/dev/vault"})
    assert f"{VAULT_SOURCE}/notes:/c/dev/vault/notes:ro" in services["agent"]["volumes"]


def test_containment_ignores_case_but_the_mount_keeps_the_vault_spelling():
    site = {"MEMORY_VAULT_PATH": "C:/Dev/Vault", "CODE_ROOT": "c:/dev", "DATA_PATH": "/srv/data"}
    agent_ro = [v for v in _compose({KEY: "notes"}, site=site)["agent"]["volumes"] if v.startswith(VAULT_SOURCE)]
    assert agent_ro == [f"{VAULT_SOURCE}/notes:/c/dev/Vault/notes:ro"]


def test_a_windows_vault_warns_that_a_differently_cased_path_bypasses_the_mount():
    for vault in ("C:/dev/vault", r"C:\dev\vault", "/c/dev/vault"):
        warnings = [w for w in _render({KEY: "notes", "MEMORY_VAULT_PATH": vault}).warnings if "differently cased" in w]
        assert len(warnings) == 1 and KEY in warnings[0], vault


def test_a_posix_vault_or_an_unset_key_does_not_warn_about_case():
    assert not any("differently cased" in w for w in _render({KEY: "notes"}).warnings)
    assert not any("differently cased" in w for w in _render({"MEMORY_VAULT_PATH": "C:/dev/vault"}).warnings)


def test_a_vault_that_is_the_bind_source_mounts_at_the_bind_target():
    services = _compose({KEY: "notes", "MEMORY_VAULT_PATH": "/srv/dev"})
    assert f"{VAULT_SOURCE}/notes:/c/dev/notes:ro" in services["agent"]["volumes"]


# ── host preflight: a missing folder is refused before Docker can create it ──────────────────────
RUNNING = {"mcp-memory-vault": {}, "agent": {}}
ENV = {KEY: "notes/Canonical,plans", "MEMORY_VAULT_PATH": "/srv/dev/vault"}


def _vault_check(services=RUNNING, env=ENV, existing=("/srv/dev/vault/notes/Canonical", "/srv/dev/vault/plans")):
    return preflight.vault_readonly_check(services, env, lambda path: path in existing)


def test_preflight_passes_when_every_folder_exists():
    check = _vault_check()
    assert check is not None and check.ok


def test_preflight_names_each_missing_folder_and_blocks():
    check = _vault_check(existing=("/srv/dev/vault/plans",))
    assert check is not None and not check.ok and check.blocking
    assert "notes/Canonical" in check.detail and "plans" not in check.detail.split("missing under")[1]


def test_preflight_has_no_check_when_unset_or_when_no_mounting_service_starts():
    assert _vault_check(env={"MEMORY_VAULT_PATH": "/srv/dev/vault"}) is None
    assert _vault_check(services={"caddy": {}}) is None


def test_host_checks_include_the_vault_check_and_refuse_a_missing_folder(tmp_path):
    facts = preflight.HostFacts(docker_error=None, compose_version="2.39.1", runtimes=frozenset({"runc"}),
                                busy_ports=frozenset(), disk_path="/var/lib/docker", disk_free_gb=500.0)
    checks = preflight.host_checks(RUNNING, ENV, facts, secret_keys=[], optional_secrets=[], secrets_path=None,
                                   model_files=[], is_dir=lambda path: False)
    failed = [c for c in checks if c.blocking and not c.ok]
    assert [c.name for c in failed] == ["vault read-only folders exist"]


@pytest.mark.skipif(os.name != "nt", reason="the /c/... spelling is only translated on a Windows host")
def test_host_path_is_dir_opens_a_git_bash_spelling_on_windows(tmp_path):
    drive, rest = str(tmp_path).replace("\\", "/").split(":", 1)
    assert preflight.host_path_is_dir(f"/{drive.lower()}{rest}")
    assert not preflight.host_path_is_dir(f"/{drive.lower()}{rest}/missing")


def test_the_rendered_compose_still_parses_as_yaml_with_the_new_mounts(tmp_path):
    _render({KEY: "notes/Canonical"}).write(tmp_path)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text(encoding="utf-8"))
    assert any(v.endswith(":/vault/notes/Canonical:ro") for v in compose["services"]["mcp-memory-vault"]["volumes"])
