"""A self-hosted Infisical project as the secret source (`site: SECRETS_BACKEND: infisical`).

`ordo secrets materialize` reads the project's environment through a read-only machine identity
(Universal Auth) and writes the same out/secrets.env + out/secrets/* contract as the SOPS store.
Writes go through an optional writer identity, or are refused pointing at the Infisical UI; they never
land in the SOPS file silently. `ordo secrets backup` copies the project into the SOPS file, which
stays the offline copy. Every test talks to a fake Infisical (an in-process transport, and once a
real local HTTP server through the stdlib transport); the SOPS tests use a throwaway age key.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from ordo import cli, infisical, remote, secret_store, wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
RENDER_MODULE = sys.modules["ordo.render"]
HARDWARE = HardwareProfile.from_spec({"gpus": [], "ram_gb": 32, "cpu_cores": 8})
BASE = {"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}}

HAVE_SOPS = shutil.which("sops") is not None and shutil.which("age-keygen") is not None
needs_sops = pytest.mark.skipif(not HAVE_SOPS, reason="sops and age-keygen are not installed")

URL = "https://infisical.example.test"
SITE = {"SECRETS_BACKEND": "infisical", "INFISICAL_URL": URL, "INFISICAL_PROJECT": "ordo-stack"}
READER = ("reader-client-id", "reader-client-secret-value")
WRITER = ("writer-client-id", "writer-client-secret-value")

NEEDS = secret_store.SecretNeeds(
    required=("OPS_CONTROLLER_TOKEN", "LITELLM_MASTER_KEY", "HF_TOKEN"),
    optional=("HF_TOKEN",),
    files=(secret_store.SecretFile(key="DISCORD_BOT_TOKEN", file="discord_token", service="agent"),),
)


class FakeInfisical:
    """The three read endpoints and the raw secret write endpoints, with per-identity permissions."""

    def __init__(self, secrets: dict[str, str] | None = None, *, hidden: bool = False):
        self.secrets: dict[str, str] = dict(secrets or {})
        self.identities = {READER[0]: (READER[1], "read"), WRITER[0]: (WRITER[1], "write")}
        self.projects = {"ordo-stack": "proj-123"}
        self.hidden = hidden
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.write_bodies: list[dict] = []
        self.deny_read = False

    def __call__(self, method, url, headers, body, timeout):
        assert timeout and timeout > 0
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == URL
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((method, path))
        payload = json.loads(body) if body else {}
        if method == "POST" and path == "/api/v1/auth/universal-auth/login":
            ident = self.identities.get(payload.get("clientId"))
            if ident is None or ident[0] != payload.get("clientSecret"):
                return 401, b'{"message":"Invalid credentials"}'
            token = f"token-for-{payload['clientId']}"
            self.tokens[token] = ident[1]
            return 200, json.dumps({"accessToken": token, "expiresIn": 7200, "tokenType": "Bearer"}).encode()
        auth = headers.get("Authorization", "")
        role = self.tokens.get(auth.removeprefix("Bearer "))
        if role is None:
            return 401, b'{"message":"Token missing"}'
        if method == "GET" and path.startswith("/api/v1/projects/slug/"):
            slug = path.rsplit("/", 1)[1]
            if slug not in self.projects:
                return 404, b'{"message":"not found"}'
            return 200, json.dumps({"id": self.projects[slug], "slug": slug, "environments": []}).encode()
        if method == "GET" and path == "/api/v3/secrets/raw":
            if self.deny_read:
                return 403, b'{"message":"forbidden"}'
            assert query == {"workspaceId": "proj-123", "environment": "prod", "secretPath": "/"}
            items = [{"secretKey": k, "secretValue": "<hidden-by-infisical>" if self.hidden else v,
                      "secretValueHidden": self.hidden, "type": "shared"} for k, v in self.secrets.items()]
            return 200, json.dumps({"secrets": items, "imports": []}).encode()
        if path.startswith("/api/v3/secrets/raw/"):
            if role != "write":
                return 403, b'{"message":"forbidden"}'
            key = urllib.parse.unquote(path.rsplit("/", 1)[1])
            assert payload["workspaceId"] == "proj-123" and payload["environment"] == "prod"
            assert payload["secretPath"] == "/" and payload["type"] == "shared"
            self.write_bodies.append({"method": method, "key": key})
            if method == "POST":
                if key in self.secrets:
                    return 400, b'{"message":"exists"}'
                self.secrets[key] = payload["secretValue"]
            elif method == "PATCH":
                if key not in self.secrets:
                    return 404, b'{"message":"missing"}'
                self.secrets[key] = payload["secretValue"]
            elif method == "DELETE":
                self.secrets.pop(key, None)
            return 200, json.dumps({"secret": {"secretKey": key}}).encode()
        return 404, b'{"message":"no route"}'


@pytest.fixture
def fake(monkeypatch):
    server = FakeInfisical({"OPS_CONTROLLER_TOKEN": "ops-token-value", "LITELLM_MASTER_KEY": "sk-master-value",
                            "HF_TOKEN": "hf-token-value", "DISCORD_BOT_TOKEN": "discord-token-value",
                            "NOT_READ_BY_RENDER": "unused-value"})
    monkeypatch.setattr(infisical, "send", server)
    return server


@pytest.fixture
def reader_env(monkeypatch):
    monkeypatch.setenv("INFISICAL_ORDO_CLIENT_ID", READER[0])
    monkeypatch.setenv("INFISICAL_ORDO_CLIENT_SECRET", READER[1])
    for name in ("INFISICAL_ORDO_WRITER_CLIENT_ID", "INFISICAL_ORDO_WRITER_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def writer_env(monkeypatch, reader_env):
    monkeypatch.setenv("INFISICAL_ORDO_WRITER_CLIENT_ID", WRITER[0])
    monkeypatch.setenv("INFISICAL_ORDO_WRITER_CLIENT_SECRET", WRITER[1])


def _store(tmp_path, site=None, **kw) -> secret_store.InfisicalStore:
    store = secret_store.store_for({**SITE, **(site or {})}, tmp_path / "out", repo_root=tmp_path / "repo", **kw)
    assert isinstance(store, secret_store.InfisicalStore)
    return store


def _values(path: Path) -> dict[str, str]:
    return secret_store.parse_dotenv(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# the site keys (strict schema)
# --------------------------------------------------------------------------- #


def test_the_infisical_backend_site_keys_validate():
    Source.from_dict({**BASE, "site": dict(SITE)})
    Source.from_dict({**BASE, "site": {**SITE, "INFISICAL_ENVIRONMENT": "staging",
                                       "SECRETS_SOURCE": "../ordo-secrets/secrets.env.sops"}})
    Source.from_dict({**BASE, "site": {"SECRETS_BACKEND": "sops",
                                       "SECRETS_SOURCE": "../ordo-secrets/secrets.env.sops"}})


@pytest.mark.parametrize("site, match", [
    ({"SECRETS_BACKEND": "vault"}, "SECRETS_BACKEND"),
    ({"SECRETS_BACKEND": "sops"}, "SECRETS_SOURCE"),
    ({"SECRETS_BACKEND": "infisical", "INFISICAL_PROJECT": "ordo-stack"}, "INFISICAL_URL"),
    ({"SECRETS_BACKEND": "infisical", "INFISICAL_URL": URL}, "INFISICAL_PROJECT"),
    ({**SITE, "INFISICAL_URL": "infisical.example.test"}, "INFISICAL_URL"),
    ({**SITE, "INFISICAL_PROJECT": "Ordo Stack"}, "INFISICAL_PROJECT"),
    ({**SITE, "INFISICAL_ENVIRONMENT": "prod/x"}, "INFISICAL_ENVIRONMENT"),
    ({"INFISICAL_URL": URL, "INFISICAL_PROJECT": "ordo-stack"}, "SECRETS_BACKEND"),
    ({**SITE, "INFISICAL_ORDO_CLIENT_SECRET": "x"}, "never in ordo.yaml"),
    ({**SITE, "INFISICAL_ORDO_WRITER_CLIENT_ID": "x"}, "never in ordo.yaml"),
])
def test_bad_backend_site_keys_fail_the_load(site, match):
    with pytest.raises(ValueError, match=match):
        Source.from_dict({**BASE, "site": site})


def test_store_for_picks_the_backend(tmp_path):
    assert isinstance(secret_store.store_for({}, tmp_path, repo_root=tmp_path), secret_store.PlainStore)
    sops = secret_store.store_for({"SECRETS_SOURCE": "x.env.sops"}, tmp_path, repo_root=tmp_path)
    assert isinstance(sops, secret_store.SopsStore)
    store = _store(tmp_path, {"SECRETS_SOURCE": "x.env.sops"})
    assert store.environment == "prod" and store.project == "ordo-stack" and store.url == URL
    assert isinstance(store.backup, secret_store.SopsStore)
    assert store.backup.path == (tmp_path / "repo" / "x.env.sops").resolve()
    assert _store(tmp_path, {"INFISICAL_ENVIRONMENT": "dev"}).environment == "dev"
    assert _store(tmp_path).backup is None


def test_store_for_refuses_an_invalid_backend_config(tmp_path):
    with pytest.raises(secret_store.SecretStoreError, match="INFISICAL_PROJECT"):
        secret_store.store_for({"SECRETS_BACKEND": "infisical", "INFISICAL_URL": URL}, tmp_path, repo_root=tmp_path)


# --------------------------------------------------------------------------- #
# the client: login, project, read
# --------------------------------------------------------------------------- #


def test_the_store_reads_the_project_environment(tmp_path, fake, reader_env):
    values = secret_store.parse_dotenv(_store(tmp_path).read_text())
    assert values["OPS_CONTROLLER_TOKEN"] == "ops-token-value" and values["NOT_READ_BY_RENDER"] == "unused-value"
    assert fake.calls == [("POST", "/api/v1/auth/universal-auth/login"),
                          ("GET", "/api/v1/projects/slug/ordo-stack"), ("GET", "/api/v3/secrets/raw")]


def test_a_trailing_slash_on_the_url_is_harmless(tmp_path, fake, reader_env):
    assert _store(tmp_path, {"INFISICAL_URL": URL + "/"}).read_text()


def test_rejected_credentials_say_so_and_never_echo_them(tmp_path, fake, monkeypatch, reader_env):
    monkeypatch.setenv("INFISICAL_ORDO_CLIENT_SECRET", "wrong-secret-value")
    with pytest.raises(secret_store.SecretStoreError, match="identity credentials rejected") as err:
        _store(tmp_path).read_text()
    assert "wrong-secret-value" not in str(err.value) and "INFISICAL_ORDO_CLIENT_ID" in str(err.value)


def test_a_forbidden_read_says_the_identity_lacks_read(tmp_path, fake, reader_env):
    fake.deny_read = True
    with pytest.raises(secret_store.SecretStoreError, match="identity lacks read on project 'ordo-stack'"):
        _store(tmp_path).read_text()


def test_an_unknown_project_is_named(tmp_path, fake, reader_env):
    with pytest.raises(secret_store.SecretStoreError, match="no-such-project"):
        _store(tmp_path, {"INFISICAL_PROJECT": "no-such-project"}).read_text()


def test_hidden_values_are_an_error_not_a_placeholder(tmp_path, monkeypatch, reader_env):
    monkeypatch.setattr(infisical, "send", FakeInfisical({"A": "x"}, hidden=True))
    with pytest.raises(secret_store.SecretStoreError, match="values are hidden"):
        _store(tmp_path).read_text()


def test_an_unreachable_server_is_a_clear_error(tmp_path, monkeypatch, reader_env):
    def refuse(method, url, headers, body, timeout):
        raise infisical.InfisicalError(f"cannot reach Infisical at {URL}: connection refused")
    monkeypatch.setattr(infisical, "send", refuse)
    with pytest.raises(secret_store.SecretStoreError, match="cannot reach Infisical"):
        _store(tmp_path).read_text()


def test_a_multi_line_value_is_refused_by_name(tmp_path, monkeypatch, reader_env):
    monkeypatch.setattr(infisical, "send", FakeInfisical({"PEM_KEY": "line-one\nline-two"}))
    with pytest.raises(secret_store.SecretStoreError, match="PEM_KEY") as err:
        _store(tmp_path).read_text()
    assert "line-one" not in str(err.value)


def test_no_credentials_names_where_to_put_them(tmp_path, fake, monkeypatch):
    for name in ("INFISICAL_ORDO_CLIENT_ID", "INFISICAL_ORDO_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(secret_store.SecretStoreError, match="INFISICAL_ORDO_CLIENT_ID.*INFISICAL_ORDO_CLIENT_SECRET"):
        _store(tmp_path).read_text()


def test_credentials_come_from_the_backup_store_and_env_wins(tmp_path, fake, monkeypatch):
    backup = secret_store.PlainStore(tmp_path / "backup.env")
    backup.write_text(f"INFISICAL_ORDO_CLIENT_ID={READER[0]}\nINFISICAL_ORDO_CLIENT_SECRET={READER[1]}\n")
    for name in ("INFISICAL_ORDO_CLIENT_ID", "INFISICAL_ORDO_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    store = secret_store.InfisicalStore(URL, "ordo-stack", "prod", backup=backup)
    assert secret_store.parse_dotenv(store.read_text())["HF_TOKEN"] == "hf-token-value"
    monkeypatch.setenv("INFISICAL_ORDO_CLIENT_SECRET", "env-wins-and-is-wrong")
    store = secret_store.InfisicalStore(URL, "ordo-stack", "prod", backup=backup)
    with pytest.raises(secret_store.SecretStoreError, match="identity credentials rejected"):
        store.read_text()


# --------------------------------------------------------------------------- #
# materialize: the same contract as the SOPS store
# --------------------------------------------------------------------------- #


def test_materialize_writes_the_render_keys_and_file_secrets(tmp_path, fake, reader_env):
    out = tmp_path / "out"
    result = secret_store.materialize(_store(tmp_path), NEEDS, out)
    values = _values(result.secrets_env)
    assert list(values) == ["OPS_CONTROLLER_TOKEN", "LITELLM_MASTER_KEY", "HF_TOKEN"]
    assert values["LITELLM_MASTER_KEY"] == "sk-master-value"
    assert "NOT_READ_BY_RENDER" not in values
    assert (out / "secrets" / "discord_token").read_text(encoding="utf-8") == "discord-token-value"
    assert "Infisical" in result.secrets_env.read_text(encoding="utf-8").splitlines()[0]


def test_materialize_fails_naming_missing_keys_and_writes_nothing(tmp_path, fake, reader_env):
    del fake.secrets["LITELLM_MASTER_KEY"]
    out = tmp_path / "out"
    with pytest.raises(secret_store.MissingSecrets) as err:
        secret_store.materialize(_store(tmp_path), NEEDS, out)
    assert err.value.keys == ["LITELLM_MASTER_KEY"]
    assert "ops-token-value" not in str(err.value)
    assert not (out / "secrets.env").exists()


def test_materialize_refuses_to_drop_a_value_infisical_lacks(tmp_path, fake, reader_env):
    out = tmp_path / "out"
    out.mkdir()
    (out / "secrets.env").write_text("OPS_CONTROLLER_TOKEN=x\nONLY_LIVE=live-only-value\n", encoding="utf-8")
    with pytest.raises(secret_store.LiveOnlySecrets) as err:
        secret_store.materialize(_store(tmp_path), NEEDS, out)
    assert "ONLY_LIVE" in str(err.value) and "live-only-value" not in str(err.value)
    assert "Infisical" in str(err.value)


# --------------------------------------------------------------------------- #
# writes: a writer identity, else a refusal pointing at the Infisical UI
# --------------------------------------------------------------------------- #


def test_a_write_without_a_writer_identity_is_refused_and_writes_nothing(tmp_path, fake, reader_env):
    store = _store(tmp_path)
    before = dict(fake.secrets)
    with pytest.raises(secret_store.SecretStoreError, match="Infisical UI") as err:
        secret_store.update(store, [], provided={"HF_TOKEN": "hf-new-value"})
    assert "HF_TOKEN" in str(err.value) and "hf-new-value" not in str(err.value)
    assert "INFISICAL_ORDO_WRITER_CLIENT_ID" in str(err.value)
    assert fake.secrets == before and fake.write_bodies == []


def test_a_writer_identity_creates_updates_and_deletes(tmp_path, fake, writer_env):
    store = _store(tmp_path)
    text = store.read_text()
    new, _, _ = secret_store.update_dotenv(text, [], provided={"HF_TOKEN": "hf-new", "BRAND_NEW": "b"},
                                           remove=["NOT_READ_BY_RENDER"])
    store.write_text(new)
    assert fake.secrets["HF_TOKEN"] == "hf-new" and fake.secrets["BRAND_NEW"] == "b"
    assert "NOT_READ_BY_RENDER" not in fake.secrets
    assert sorted((b["method"], b["key"]) for b in fake.write_bodies) == [
        ("DELETE", "NOT_READ_BY_RENDER"), ("PATCH", "HF_TOKEN"), ("POST", "BRAND_NEW")]


def test_an_unchanged_write_sends_nothing(tmp_path, fake, writer_env):
    store = _store(tmp_path)
    store.write_text(store.read_text())
    assert fake.write_bodies == []


def test_a_writer_without_write_permission_says_so(tmp_path, fake, writer_env):
    fake.identities[WRITER[0]] = (WRITER[1], "read")
    with pytest.raises(secret_store.SecretStoreError, match="writer identity lacks write on project"):
        secret_store.update(_store(tmp_path), [], provided={"HF_TOKEN": "hf-new"})


# --------------------------------------------------------------------------- #
# the CLI end to end (a fresh `ordo init` + render, then pointed at Infisical)
# --------------------------------------------------------------------------- #


@pytest.fixture
def stack(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(wizard, "detect", lambda: HARDWARE)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: HARDWARE)
    emails = tmp_path / "emails.txt"
    emails.write_text(remote.ALLOWLIST_PLACEHOLDER + "\n", encoding="utf-8")
    monkeypatch.setattr(remote, "ALLOWLIST_PATH", emails)
    monkeypatch.setattr(remote, "CERT_DIR", tmp_path / "certs")
    out = tmp_path / "out"
    wizard.run(CATALOG, REGISTRY, out, interactive=False, answers={}, host_root=tmp_path / "repo")
    assert cli.main(["--source", str(out / "ordo.yaml"), "render", "--out", str(out)]) == 0
    # Infisical holds every value the local install generated.
    fake.secrets = {k: v for k, v in _values(out / "secrets.env").items() if v}
    return out


def _point_at_infisical(out: Path, **extra: str) -> None:
    source = out / "ordo.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["site"] = {**(data.get("site") or {}), **SITE, **extra}
    source.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _ordo(out: Path, *argv: str) -> int:
    return cli.main(["secrets", *argv, "--out", str(out), "--source", str(out / "ordo.yaml")])


def test_cli_materialize_and_list_with_infisical(stack, fake, reader_env, capsys):
    _point_at_infisical(stack)
    fake.secrets["OPS_CONTROLLER_TOKEN"] = "rotated-in-infisical"
    capsys.readouterr()
    assert _ordo(stack, "materialize") == 0
    assert _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"] == "rotated-in-infisical"
    assert _ordo(stack, "list") == 0
    printed = capsys.readouterr().out
    assert "backend: infisical" in printed and "ordo-stack" in printed
    assert "OPS_CONTROLLER_TOKEN" in printed
    for value in fake.secrets.values():
        assert value not in printed


def test_cli_set_without_a_writer_refuses_and_leaves_everything(stack, fake, reader_env, monkeypatch, capsys):
    _point_at_infisical(stack)
    before_live = (stack / "secrets.env").read_text(encoding="utf-8")
    before = dict(fake.secrets)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hf-typed-value\n"))
    capsys.readouterr()
    assert _ordo(stack, "set", "HF_TOKEN", "--from-stdin") == 1
    err = capsys.readouterr().err
    assert "Infisical UI" in err and "hf-typed-value" not in err
    assert fake.secrets == before
    assert (stack / "secrets.env").read_text(encoding="utf-8") == before_live


def test_cli_set_and_rotate_with_a_writer(stack, fake, writer_env, monkeypatch, capsys):
    _point_at_infisical(stack)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hf-typed-value\n"))
    capsys.readouterr()
    assert _ordo(stack, "set", "HF_TOKEN", "--from-stdin") == 0
    assert fake.secrets["HF_TOKEN"] == "hf-typed-value"
    assert _values(stack / "secrets.env")["HF_TOKEN"] == "hf-typed-value"
    before = fake.secrets["OPS_CONTROLLER_TOKEN"]
    assert _ordo(stack, "rotate", "OPS_CONTROLLER_TOKEN") == 0
    after = fake.secrets["OPS_CONTROLLER_TOKEN"]
    assert after != before and _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"] == after
    printed = capsys.readouterr().out
    assert "hf-typed-value" not in printed and after not in printed


def test_cli_import_is_refused_under_infisical(stack, reader_env, capsys):
    _point_at_infisical(stack)
    assert _ordo(stack, "import", "--to", str(stack.parent / "x.env.sops")) == 1
    assert "ordo secrets backup" in capsys.readouterr().err


def test_cli_backup_needs_the_infisical_backend_and_a_sops_file(stack, reader_env, capsys):
    assert _ordo(stack, "backup") == 1
    assert "SECRETS_BACKEND" in capsys.readouterr().err
    _point_at_infisical(stack)
    assert _ordo(stack, "backup") == 1
    assert "SECRETS_SOURCE" in capsys.readouterr().err


def test_up_materializes_from_infisical(stack, fake, reader_env):
    import argparse

    _point_at_infisical(stack)
    (stack / "secrets.env").unlink()
    args = argparse.Namespace(source=str(stack / "ordo.yaml"), source_explicit=True, out=str(stack))
    assert cli._prepare_secrets(args, stack) == 0
    materialized = _values(stack / "secrets.env")
    assert list(materialized)[: len(cli._manifest(stack)["required_secrets"])] == \
        cli._manifest(stack)["required_secrets"]
    assert all(materialized[k] == fake.secrets.get(k, "") for k in materialized)


# --------------------------------------------------------------------------- #
# SOPS as the offline backup (throwaway age key)
# --------------------------------------------------------------------------- #


@pytest.fixture
def age_key(tmp_path, monkeypatch):
    key_file = tmp_path / "age" / "keys.txt"
    key_file.parent.mkdir()
    subprocess.run(["age-keygen", "-o", str(key_file)], check=True, capture_output=True)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))
    monkeypatch.delenv("SOPS_AGE_RECIPIENTS", raising=False)
    return key_file


@needs_sops
def test_backup_copies_the_project_into_sops_and_keeps_sops_only_keys(stack, fake, reader_env, tmp_path,
                                                                      age_key, capsys):
    target = tmp_path / "ordo.env.sops"
    sops = secret_store.SopsStore(target)
    sops.write_text(f"INFISICAL_ORDO_CLIENT_ID={READER[0]}\nINFISICAL_ORDO_CLIENT_SECRET={READER[1]}\n"
                    "OPS_CONTROLLER_TOKEN=stale-value\n")
    _point_at_infisical(stack, SECRETS_SOURCE=str(target))
    capsys.readouterr()
    assert _ordo(stack, "backup") == 0
    printed = capsys.readouterr().out
    stored = secret_store.parse_dotenv(sops.read_text())
    for key, value in fake.secrets.items():
        assert stored[key] == value
        assert value not in printed
    assert stored["INFISICAL_ORDO_CLIENT_SECRET"] == READER[1]          # the bootstrap pair stays
    assert "updated" in printed and "OPS_CONTROLLER_TOKEN" in printed
    mtime = target.stat().st_mtime_ns
    assert _ordo(stack, "backup") == 0                                   # a no-op does not re-encrypt
    assert target.stat().st_mtime_ns == mtime
    assert "already matches" in capsys.readouterr().out
    assert _ordo(stack, "list") == 0
    listed = capsys.readouterr().out
    assert "backed up" in listed and READER[1] not in listed


@needs_sops
def test_bootstrap_credentials_are_read_from_the_sops_file(stack, fake, tmp_path, age_key, monkeypatch):
    for name in ("INFISICAL_ORDO_CLIENT_ID", "INFISICAL_ORDO_CLIENT_SECRET",
                 "INFISICAL_ORDO_WRITER_CLIENT_ID", "INFISICAL_ORDO_WRITER_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    target = tmp_path / "ordo.env.sops"
    secret_store.SopsStore(target).write_text(
        f"INFISICAL_ORDO_CLIENT_ID={READER[0]}\nINFISICAL_ORDO_CLIENT_SECRET={READER[1]}\n")
    _point_at_infisical(stack, SECRETS_SOURCE=str(target))
    assert _ordo(stack, "materialize") == 0


@needs_sops
def test_setting_a_bootstrap_key_writes_the_sops_file_not_infisical(stack, fake, reader_env, tmp_path, age_key,
                                                                    monkeypatch, capsys):
    target = tmp_path / "ordo.env.sops"
    secret_store.SopsStore(target).write_text("PLACEHOLDER_KEY=x\n")
    _point_at_infisical(stack, SECRETS_SOURCE=str(target))
    monkeypatch.setattr(sys, "stdin", io.StringIO("new-writer-secret\n"))
    capsys.readouterr()
    assert _ordo(stack, "set", "INFISICAL_ORDO_WRITER_CLIENT_SECRET", "--from-stdin") == 0
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    assert stored["INFISICAL_ORDO_WRITER_CLIENT_SECRET"] == "new-writer-secret"
    assert "INFISICAL_ORDO_WRITER_CLIENT_SECRET" not in fake.secrets and fake.write_bodies == []
    assert str(target.name) in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# the stdlib transport against a real local HTTP server
# --------------------------------------------------------------------------- #


def test_the_urllib_transport_talks_to_a_real_server(tmp_path, reader_env):
    fake = FakeInfisical({"OPS_CONTROLLER_TOKEN": "over-http"})

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            status, payload = fake(self.command, URL + self.path, dict(self.headers.items()), body, 5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local = f"http://127.0.0.1:{server.server_address[1]}"
        store = secret_store.InfisicalStore(local, "ordo-stack", "prod")
        assert secret_store.parse_dotenv(store.read_text()) == {"OPS_CONTROLLER_TOKEN": "over-http"}
        fake.deny_read = True
        with pytest.raises(secret_store.SecretStoreError, match="identity lacks read"):
            secret_store.InfisicalStore(local, "ordo-stack", "prod").read_text()
    finally:
        server.shutdown()
        server.server_close()
    closed = secret_store.InfisicalStore(local, "ordo-stack", "prod")
    with pytest.raises(secret_store.SecretStoreError, match="cannot reach Infisical"):
        closed.read_text()
