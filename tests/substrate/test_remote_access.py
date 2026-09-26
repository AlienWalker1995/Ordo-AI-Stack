"""`ordo remote enable` / `ordo remote disable`: remote access is a later, reversible opt-in.

`ordo init` asks nothing about Tailscale or Google. `ordo remote enable` collects the tailnet
hostname, the Caddy bind, the Google OAuth client and the allowlist, writes them to the source's
`site:` (the allowlist too, as SSO_ALLOWED_EMAILS) and to secrets.env, and re-renders; the render
writes the allowlist file oauth2-proxy mounts into out/. `ordo remote disable` undoes it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from ordo import cli
from ordo.host import remote, wizard
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.hardware import HardwareProfile
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
RENDER_MODULE = sys.modules["ordo.render.engine"]
HARDWARE = HardwareProfile.from_spec({"gpus": [], "ram_gb": 32, "cpu_cores": 8})

CLIENT_SECRET = "GOCSPX-never-print-me"
ENABLE_FLAGS = ["--yes", "--hostname", "ordo.tail1234.ts.net", "--bind", "100.64.0.1",
                "--client-id", "cid.apps.googleusercontent.com", "--client-secret", CLIENT_SECRET,
                "--emails", "me@example.com, you@example.com"]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A fresh `ordo init --yes` config in tmp_path/out."""
    monkeypatch.setattr(wizard, "detect", lambda: HARDWARE)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: HARDWARE)
    monkeypatch.setattr(remote, "CERT_DIR", tmp_path / "certs")
    out = tmp_path / "out"
    wizard.run(CATALOG, REGISTRY, out, interactive=False, answers={}, host_root=tmp_path / "repo")
    return out


def _secrets(out: Path) -> dict[str, str]:
    lines = (out / "secrets.env").read_text(encoding="utf-8").splitlines()
    return dict(ln.split("=", 1) for ln in lines if ln and not ln.startswith("#"))


def _compose(out: Path) -> dict:
    return yaml.safe_load((out / "docker-compose.yml").read_text(encoding="utf-8"))


def test_enable_writes_site_secrets_and_renders_the_edge_with_its_allowlist(stack, capsys):
    assert cli.main(["remote", "enable", "--out", str(stack), *ENABLE_FLAGS]) == 0
    site = Source.load(stack / "ordo.yaml").site
    assert site["CADDY_TAILNET_HOSTNAME"] == "ordo.tail1234.ts.net"
    assert site["CADDY_TAILNET_DOMAIN"] == "tail1234.ts.net"
    assert site["CADDY_BIND"] == "100.64.0.1"
    secrets = _secrets(stack)
    assert secrets["OAUTH2_PROXY_CLIENT_ID"] == "cid.apps.googleusercontent.com"
    assert secrets["OAUTH2_PROXY_CLIENT_SECRET"] == CLIENT_SECRET
    assert secrets["OAUTH2_PROXY_COOKIE_SECRET"]              # generated, not asked
    assert secrets["LITELLM_KEY_EDGE"]                        # the edge's own gateway key, generated
    assert site["SSO_ALLOWED_EMAILS"] == "me@example.com,you@example.com"
    allowlist = stack / "oauth2-proxy" / "emails.txt"
    assert allowlist.read_text(encoding="utf-8") == "me@example.com\nyou@example.com\n"
    services = _compose(stack)["services"]
    assert "caddy" in services and "ports" not in services["dashboard"]
    printed = capsys.readouterr().out
    assert CLIENT_SECRET not in printed and "ordo up --all" in printed
    assert "still blank" not in printed         # nothing left that would block `ordo up`


def test_enable_keeps_the_existing_secrets(stack):
    before = _secrets(stack)
    assert cli.main(["remote", "enable", "--out", str(stack), *ENABLE_FLAGS]) == 0
    after = _secrets(stack)
    assert all(after[key] == value for key, value in before.items())


def test_disable_restores_local_access(stack):
    source_before = (stack / "ordo.yaml").read_text(encoding="utf-8")
    assert cli.main(["remote", "enable", "--out", str(stack), *ENABLE_FLAGS]) == 0
    assert cli.main(["remote", "disable", "--out", str(stack), "--yes"]) == 0
    assert (stack / "ordo.yaml").read_text(encoding="utf-8") == source_before
    secrets = _secrets(stack)
    assert "OAUTH2_PROXY_CLIENT_ID" not in secrets and "OAUTH2_PROXY_CLIENT_SECRET" not in secrets
    assert not (stack / "oauth2-proxy").exists()
    services = _compose(stack)["services"]
    assert "caddy" not in services and services["dashboard"]["ports"] == ["127.0.0.1:8444:8080"]


def test_enable_and_disable_edit_an_explicit_plugins_list(stack, tmp_path):
    wizard.run(CATALOG, REGISTRY, stack, interactive=False, answers={"features": "chat"},
               host_root=tmp_path / "repo")
    assert "edge" not in Source.load(stack / "ordo.yaml").plugins
    assert cli.main(["remote", "enable", "--out", str(stack), *ENABLE_FLAGS]) == 0
    assert "edge" in Source.load(stack / "ordo.yaml").plugins
    assert cli.main(["remote", "disable", "--out", str(stack), "--yes"]) == 0
    assert "edge" not in Source.load(stack / "ordo.yaml").plugins


@pytest.mark.parametrize("flag, value", [("--hostname", "https://ordo.ts.net"), ("--emails", "not-an-email"),
                                         ("--client-id", "")])
def test_invalid_input_writes_nothing(stack, flag, value):
    before = {p.name: p.read_text(encoding="utf-8") for p in stack.iterdir() if p.is_file()}
    flags = list(ENABLE_FLAGS)
    flags[flags.index(flag) + 1] = value
    assert cli.main(["remote", "enable", "--out", str(stack), *flags]) == 1
    assert {p.name: p.read_text(encoding="utf-8") for p in stack.iterdir() if p.is_file()} == before


def test_yes_reads_the_client_secret_from_the_environment(stack, monkeypatch):
    monkeypatch.setenv("OAUTH2_PROXY_CLIENT_SECRET", "from-env")
    flags = [f for f in ENABLE_FLAGS if f not in ("--client-secret", CLIENT_SECRET)]
    assert cli.main(["remote", "enable", "--out", str(stack), *flags]) == 0
    assert _secrets(stack)["OAUTH2_PROXY_CLIENT_SECRET"] == "from-env"


def test_the_rendered_stack_after_enable_is_the_source(stack):
    assert cli.main(["remote", "enable", "--out", str(stack), *ENABLE_FLAGS]) == 0
    rc = render(Source.load(stack / "ordo.yaml"), CATALOG, REGISTRY)
    assert "edge" in rc.plugins_enabled
    assert set(_secrets(stack)) >= set(rc.required_secrets)


def test_the_tailscale_cert_lands_where_caddy_reads_it():
    argv = remote.tailscale_cert_argv("ordo.tail1234.ts.net")
    assert argv[:2] == ["tailscale", "cert"]
    assert argv[argv.index("--cert-file") + 1].endswith("auth/caddy/certs/tailnet.crt")
    assert argv[argv.index("--key-file") + 1].endswith("auth/caddy/certs/tailnet.key")
    assert argv[-1] == "ordo.tail1234.ts.net"


# --- `ordo init` points here instead of asking ---


def test_init_yes_prints_the_choice_and_the_remote_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wizard, "detect", lambda: HARDWARE)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: HARDWARE)
    assert cli.main(["init", "--yes", "--out", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "Model" in printed and "Plugins" in printed and "open-webui" in printed
    assert "ordo remote enable" in printed
    assert "ordo up --all" in printed


def test_the_edge_stays_off_without_an_allowlist():
    """oauth2-proxy with no allowlisted address denies everyone, so the edge needs the list."""
    site = {"CADDY_BIND": "100.64.0.1", "CADDY_TAILNET_HOSTNAME": "ordo.tail1234.ts.net",
            "CADDY_TAILNET_DOMAIN": "tail1234.ts.net"}
    source = {"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}, "model": "auto", "site": site}
    rc = render(Source.from_dict(source), CATALOG, REGISTRY)
    assert "edge" not in rc.plugins_enabled
    assert any("SSO_ALLOWED_EMAILS" in w for w in rc.warnings)


def test_an_allowlist_change_recreates_oauth2_proxy():
    """oauth2-proxy reads the allowlist through a bind compose does not hash, so the rendered
    service carries the allowlist's digest: a changed list changes its config and `ordo apply`
    recreates it."""
    from ordo.render import compose

    def label(emails: str) -> str:
        site = {"CADDY_BIND": "100.64.0.1", "CADDY_TAILNET_HOSTNAME": "ordo.tail1234.ts.net",
                "CADDY_TAILNET_DOMAIN": "tail1234.ts.net", "SSO_ALLOWED_EMAILS": emails}
        source = {"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}, "model": "auto", "site": site}
        services = render(Source.from_dict(source), CATALOG, REGISTRY).compose_dict()["services"]
        return services["oauth2-proxy"]["labels"][compose.RENDERED_CONFIG_LABEL]

    assert label("me@example.com") == label("me@example.com")
    assert label("me@example.com") != label("me@example.com,you@example.com")
