"""One secret store: a SOPS (age) dotenv file in a private repo, with out/secrets.env materialized.

`ordo secrets materialize` writes out/secrets.env from the store (exactly the keys the render needs)
and the agent's file-form secrets under out/secrets/. `set`, `rotate` and `import` edit the store and
never print a value. Without a configured SOPS file (a fresh local install) out/secrets.env itself is
the store, as before. The SOPS tests use a throwaway age key made in the test's temp dir.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ordo import cli, remote, secret_store, wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
RENDER_MODULE = sys.modules["ordo.render"]
HARDWARE = HardwareProfile.from_spec({"gpus": [], "ram_gb": 32, "cpu_cores": 8})

HAVE_SOPS = shutil.which("sops") is not None and shutil.which("age-keygen") is not None
needs_sops = pytest.mark.skipif(not HAVE_SOPS, reason="sops and age-keygen are not installed")

NEEDS = secret_store.SecretNeeds(
    required=("OPS_CONTROLLER_TOKEN", "LITELLM_MASTER_KEY", "HF_TOKEN"),
    optional=("HF_TOKEN",),
    files=(secret_store.SecretFile(key="DISCORD_BOT_TOKEN", file="discord_token", service="agent"),),
)


def _values(path: Path) -> dict[str, str]:
    return secret_store.parse_dotenv(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# dotenv editing: the pure core every store shares
# --------------------------------------------------------------------------- #


def test_update_keeps_existing_values_and_mints_missing_internal_ones():
    text = "# note\nLITELLM_MASTER_KEY=sk-keep\nOLD_UNRELATED=x\n"
    new, generated, blank = secret_store.update_dotenv(
        text, ["LITELLM_MASTER_KEY", "OAUTH2_PROXY_COOKIE_SECRET", "OAUTH2_PROXY_CLIENT_ID"],
        provided={"OAUTH2_PROXY_CLIENT_ID": "cid"})
    values = secret_store.parse_dotenv(new)
    assert new.startswith("# note\n")
    assert values["LITELLM_MASTER_KEY"] == "sk-keep" and values["OLD_UNRELATED"] == "x"
    assert values["OAUTH2_PROXY_CLIENT_ID"] == "cid"
    assert values["OAUTH2_PROXY_COOKIE_SECRET"] and generated == ["OAUTH2_PROXY_COOKIE_SECRET"]
    assert blank == []


def test_update_leaves_an_external_key_blank_and_removes_keys():
    new, generated, blank = secret_store.update_dotenv("A=1\nB=2\n", ["TS_AUTHKEY"], remove=["B"])
    values = secret_store.parse_dotenv(new)
    assert values == {"A": "1", "TS_AUTHKEY": ""}
    assert generated == [] and blank == ["TS_AUTHKEY"]


def test_values_keep_everything_after_the_first_equals_sign():
    assert secret_store.parse_dotenv("K=a=b==\n# c=d\n\nJ=\n") == {"K": "a=b==", "J": ""}


# --------------------------------------------------------------------------- #
# where the store is
# --------------------------------------------------------------------------- #


def test_without_a_secrets_source_out_secrets_env_is_the_store(tmp_path):
    store = secret_store.store_for({}, tmp_path / "out", repo_root=tmp_path / "repo")
    assert isinstance(store, secret_store.PlainStore)
    assert store.path == tmp_path / "out" / "secrets.env"


def test_a_relative_secrets_source_resolves_against_the_checkout(tmp_path):
    store = secret_store.store_for({"SECRETS_SOURCE": "../ordo-personal/secrets/ordo.env.sops"},
                                   tmp_path / "out", repo_root=tmp_path / "repo")
    assert isinstance(store, secret_store.SopsStore)
    assert store.path == (tmp_path / "ordo-personal" / "secrets" / "ordo.env.sops").resolve()


def test_secrets_source_is_validated_by_the_source_schema():
    base = {"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}}
    Source.from_dict({**base, "site": {"SECRETS_SOURCE": "../ordo-personal/secrets/ordo.env.sops"}})
    with pytest.raises(ValueError, match="SECRETS_SOURCE"):
        Source.from_dict({**base, "site": {"SECRETS_SOURCE": ""}})
    with pytest.raises(ValueError, match="SECRETS_SOURCE"):
        Source.from_dict({**base, "site": {"SECRETS_SOURCE": "secrets.env"}})


def test_the_v1_operator_secrets_dir_is_retired():
    base = {"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}}
    with pytest.raises(ValueError, match="OPERATOR_SECRETS_DIR.*ordo secrets import"):
        Source.from_dict({**base, "site": {"OPERATOR_SECRETS_DIR": "/somewhere"}})


# --------------------------------------------------------------------------- #
# materialize, with out/secrets.env as the store (the fresh local install)
# --------------------------------------------------------------------------- #


def test_plain_materialize_writes_the_file_secrets_and_leaves_secrets_env_alone(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    text = "# mine\nOPS_CONTROLLER_TOKEN=o\nLITELLM_MASTER_KEY=sk-m\nDISCORD_BOT_TOKEN=d\nEXTRA=e\n"
    (out / "secrets.env").write_text(text, encoding="utf-8")
    store = secret_store.PlainStore(out / "secrets.env")
    result = secret_store.materialize(store, NEEDS, out)
    assert (out / "secrets.env").read_text(encoding="utf-8") == text
    assert (out / "secrets" / "discord_token").read_text(encoding="utf-8") == "d"
    assert result.blank_required == []


def test_materialize_fails_naming_missing_required_keys_only(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "secrets.env").write_text("OPS_CONTROLLER_TOKEN=secret-value-1\n", encoding="utf-8")
    store = secret_store.PlainStore(out / "secrets.env")
    with pytest.raises(secret_store.MissingSecrets) as err:
        secret_store.materialize(store, NEEDS, out)
    assert err.value.keys == ["LITELLM_MASTER_KEY"]          # HF_TOKEN is optional
    assert "secret-value-1" not in str(err.value)


def test_an_absent_file_secret_materializes_as_an_empty_file(tmp_path):
    """The bind source must be a file: a missing one makes Docker create a directory in its place."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "secrets.env").write_text("OPS_CONTROLLER_TOKEN=o\nLITELLM_MASTER_KEY=m\n", encoding="utf-8")
    secret_store.materialize(secret_store.PlainStore(out / "secrets.env"), NEEDS, out)
    assert (out / "secrets" / "discord_token").read_text(encoding="utf-8") == ""


def test_the_agent_entrypoint_treats_an_empty_file_secret_as_unset():
    entrypoint = (ROOT / "services" / "hermes" / "entrypoint.sh").read_text(encoding="utf-8")
    assert '[ -s "$DISCORD_BOT_TOKEN_FILE" ]' in entrypoint
    assert '[ -s "$GITHUB_BACKUP_PAT_FILE" ]' in entrypoint


# --------------------------------------------------------------------------- #
# the agent's file-form secrets are materialized from the store, not a V1 host directory
# --------------------------------------------------------------------------- #


def test_the_agent_file_secrets_name_their_store_key_and_live_under_out():
    from ordo.agents import AgentRegistry

    hermes = AgentRegistry.load(ROOT / "services").get("hermes")
    assert {s["key"] for s in hermes.secret_files} == {"DISCORD_BOT_TOKEN", "GITHUB_BACKUP_PAT"}
    for entry in hermes.secret_files:
        assert entry["source"] == "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out/secrets/" + entry["file"]


def test_the_manifest_lists_the_file_secrets_by_key():
    source = Source.from_dict({"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8},
                               "site": {"BASE_PATH": "/srv/ordo", "DATA_PATH": "/srv/ordo/data",
                                        "MEMORY_VAULT_PATH": "/srv/ordo/data/vault"}})
    manifest = RENDER_MODULE.render(source, CATALOG, REGISTRY).manifest()
    assert {"key": "DISCORD_BOT_TOKEN", "file": "discord_token", "service": "agent"} in manifest["secret_files"]


def test_no_v1_secret_path_is_left():
    assert not (ROOT / "scripts" / "secrets" / "decrypt.sh").exists()
    assert not (ROOT / "scripts" / "secrets" / "rotate-internal.sh").exists()
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    offenders = []
    for name in tracked.splitlines():
        path = ROOT / name
        if path.suffix not in {".py", ".yaml", ".yml", ".md", ".sh", ".json"} or not path.is_file():
            continue
        if name == "tests/substrate/test_secret_store.py" or name == "CHANGELOG.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(retired in text for retired in (".ai-toolkit/runtime", "rotate-internal.sh", "secrets/decrypt.sh")):
            offenders.append(name)
        # The retired site key may be named where it is refused and migrated (ordo/, the runbook),
        # never where it would still configure something.
        if "OPERATOR_SECRETS_DIR" in text and (name.startswith("services/") or name == "ordo.example.yaml"):
            offenders.append(name)
    assert offenders == []


# --------------------------------------------------------------------------- #
# rotation policy (what rotate-internal.sh encoded, now in one place)
# --------------------------------------------------------------------------- #


def test_generate_once_keys_are_never_rotated():
    for key in ("LITELLM_SALT_KEY", "LANGFUSE_SALT", "LANGFUSE_ENCRYPTION_KEY", "LIVESYNC_E2EE_PASSPHRASE"):
        assert "never rotate" in secret_store.rotation_refusal(key)


def test_keys_another_system_issues_are_not_rotated_from_the_store():
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_ADMIN_PASSWORD", "HF_TOKEN",
                "OAUTH2_PROXY_CLIENT_SECRET"):
        assert secret_store.rotation_refusal(key)


def test_the_internal_set_matches_the_retired_script():
    for key in ("LITELLM_MASTER_KEY", "LITELLM_DB_PASSWORD", "OPS_CONTROLLER_TOKEN", "THROUGHPUT_RECORD_TOKEN",
                "OAUTH2_PROXY_COOKIE_SECRET", "LITELLM_KEY_EVALS", "HERMES_API_SERVER_KEY",
                "LANGFUSE_DB_PASSWORD", "LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_REDIS_AUTH",
                "LANGFUSE_MINIO_SECRET", "LANGFUSE_NEXTAUTH_SECRET"):
        assert secret_store.rotation_refusal(key) is None, key
        assert secret_store.is_internal(key), key
    assert not secret_store.is_internal("COUCHDB_PASSWORD")


# --------------------------------------------------------------------------- #
# the CLI, end to end on a fresh local install (out/secrets.env is the store)
# --------------------------------------------------------------------------- #


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """`ordo init --yes` then `ordo render`, in tmp_path/out."""
    monkeypatch.setattr(wizard, "detect", lambda: HARDWARE)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: HARDWARE)
    emails = tmp_path / "emails.txt"
    emails.write_text(remote.ALLOWLIST_PLACEHOLDER + "\n", encoding="utf-8")
    monkeypatch.setattr(remote, "ALLOWLIST_PATH", emails)
    monkeypatch.setattr(remote, "CERT_DIR", tmp_path / "certs")
    out = tmp_path / "out"
    wizard.run(CATALOG, REGISTRY, out, interactive=False, answers={}, host_root=tmp_path / "repo")
    assert cli.main(["--source", str(out / "ordo.yaml"), "render", "--out", str(out)]) == 0
    return out


def _ordo(out: Path, *argv: str) -> int:
    return cli.main(["secrets", *argv, "--out", str(out), "--source", str(out / "ordo.yaml")])


def test_list_says_out_secrets_env_is_the_store_and_prints_names_only(stack, capsys):
    capsys.readouterr()
    assert _ordo(stack, "list") == 0
    printed = capsys.readouterr().out
    assert "no SECRETS_SOURCE" in printed
    assert "OPS_CONTROLLER_TOKEN" in printed
    for value in _values(stack / "secrets.env").values():
        if value:
            assert value not in printed


def test_set_from_stdin_updates_the_store_and_prints_the_recreate(stack, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("hf_new_value\n"))
    capsys.readouterr()
    assert _ordo(stack, "set", "HF_TOKEN", "--from-stdin") == 0
    assert _values(stack / "secrets.env")["HF_TOKEN"] == "hf_new_value"
    printed = capsys.readouterr().out
    assert "hf_new_value" not in printed
    assert "ordo recreate --reading HF_TOKEN" in printed


def test_set_generate_refuses_an_externally_issued_key(stack, capsys):
    assert _ordo(stack, "set", "HF_TOKEN", "--generate") == 1
    assert "HF_TOKEN" in capsys.readouterr().err


def test_rotate_changes_the_value_and_names_the_readers_command(stack, capsys):
    before = _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"]
    capsys.readouterr()
    assert _ordo(stack, "rotate", "OPS_CONTROLLER_TOKEN") == 0
    after = _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"]
    printed = capsys.readouterr().out
    assert after and after != before
    assert before not in printed and after not in printed
    assert "ordo recreate --reading OPS_CONTROLLER_TOKEN" in printed


def test_rotate_refuses_a_salt_and_writes_nothing(stack, capsys):
    before = (stack / "secrets.env").read_text(encoding="utf-8")
    assert _ordo(stack, "rotate", "OPS_CONTROLLER_TOKEN", "LITELLM_SALT_KEY") == 1
    assert (stack / "secrets.env").read_text(encoding="utf-8") == before
    assert "LITELLM_SALT_KEY" in capsys.readouterr().err


def test_rotate_internal_rotates_every_internal_key_present(stack, capsys):
    before = _values(stack / "secrets.env")
    assert _ordo(stack, "rotate", "--internal") == 0
    after = _values(stack / "secrets.env")
    for key, value in before.items():
        if secret_store.is_internal(key) and value:
            assert after[key] != value, key
        else:
            assert after[key] == value, key


def test_up_mints_the_local_sign_in_secret_through_the_store(stack):
    sign_in = cli._dashboard_sign_in(stack)
    lines = [ln for ln in (stack / "secrets.env").read_text(encoding="utf-8").splitlines()
             if not ln.startswith(sign_in["secret"] + "=")]
    (stack / "secrets.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = secret_store.PlainStore(stack / "secrets.env")
    assert cli._ensure_local_sign_in_secret(stack, store) is True
    assert _values(stack / "secrets.env")[sign_in["secret"]]


# --------------------------------------------------------------------------- #
# SOPS: the store in a private repo (throwaway age key)
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
def test_sops_store_round_trips_and_keeps_no_plaintext_on_disk(tmp_path, age_key):
    path = tmp_path / "private" / "secrets" / "ordo.env.sops"
    store = secret_store.SopsStore(path)
    store.write_text("A=value-one\nB=x=y\n")
    ciphertext = path.read_text(encoding="utf-8")
    assert "value-one" not in ciphertext and "sops_age__list_0__map_recipient=" in ciphertext
    assert "\r\n" not in path.read_bytes().decode("utf-8")
    assert secret_store.parse_dotenv(store.read_text()) == {"A": "value-one", "B": "x=y"}
    assert sorted(p.name for p in path.parent.iterdir()) == ["ordo.env.sops"]


@needs_sops
def test_sops_store_keeps_the_existing_recipients(tmp_path, age_key):
    second = tmp_path / "second.txt"
    subprocess.run(["age-keygen", "-o", str(second)], check=True, capture_output=True)
    second_pub = secret_store.age_public_key(second)
    path = tmp_path / "ordo.env.sops"
    store = secret_store.SopsStore(path)
    store.write_text("A=1\n", recipients=[secret_store.age_public_key(age_key), second_pub])
    store.write_text("A=2\n")
    assert second_pub in path.read_text(encoding="utf-8")


@needs_sops
def test_import_then_materialize_carries_every_live_value_over(stack, tmp_path, age_key, capsys):
    live = _values(stack / "secrets.env")
    target = tmp_path / "private" / "ordo.env.sops"
    capsys.readouterr()
    assert _ordo(stack, "import", "--to", str(target)) == 0
    printed = capsys.readouterr().out
    assert all(value not in printed for value in live.values() if value)
    assert Path(Source.load(stack / "ordo.yaml").site["SECRETS_SOURCE"]) == target
    assert secret_store.parse_dotenv(secret_store.SopsStore(target).read_text()) == {
        k: v for k, v in live.items() if v}

    (stack / "secrets.env").unlink()
    assert _ordo(stack, "materialize") == 0
    materialized = _values(stack / "secrets.env")
    manifest = cli._manifest(stack)
    assert list(materialized) == manifest["required_secrets"]
    assert all(materialized[k] == live[k] for k in materialized)


@needs_sops
def test_import_adds_only_what_the_store_lacks_and_names_differences(stack, tmp_path, age_key, capsys):
    target = tmp_path / "ordo.env.sops"
    secret_store.SopsStore(target).write_text("OPS_CONTROLLER_TOKEN=store-wins\n",
                                              recipients=[secret_store.age_public_key(age_key)])
    capsys.readouterr()
    assert _ordo(stack, "import", "--to", str(target)) == 0
    printed = capsys.readouterr().out
    assert "OPS_CONTROLLER_TOKEN" in printed and "store-wins" not in printed
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    assert stored["OPS_CONTROLLER_TOKEN"] == "store-wins"
    assert stored["LITELLM_MASTER_KEY"] == _values(stack / "secrets.env")["LITELLM_MASTER_KEY"]


@needs_sops
def test_import_takes_the_file_secrets_and_drops_the_retired_site_key(stack, tmp_path, age_key):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "discord_token").write_text("discord-value\n", encoding="utf-8")
    text = (stack / "ordo.yaml").read_text(encoding="utf-8")
    from ordo.source_edit import edit_site_keys
    (stack / "ordo.yaml").write_text(edit_site_keys(text, {"OPERATOR_SECRETS_DIR": str(legacy)}, []),
                                     encoding="utf-8")
    target = tmp_path / "ordo.env.sops"
    assert _ordo(stack, "import", "--to", str(target)) == 0
    site = yaml.safe_load((stack / "ordo.yaml").read_text(encoding="utf-8"))["site"]
    assert "OPERATOR_SECRETS_DIR" not in site
    assert secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())["DISCORD_BOT_TOKEN"] == \
        "discord-value"


@needs_sops
def test_materialize_refuses_to_drop_a_live_only_value(stack, tmp_path, age_key, capsys):
    target = tmp_path / "ordo.env.sops"
    assert _ordo(stack, "import", "--to", str(target)) == 0
    with open(stack / "secrets.env", "a", encoding="utf-8") as f:
        f.write("HAND_ADDED=only-here\n")
    capsys.readouterr()
    assert _ordo(stack, "materialize") == 1
    err = capsys.readouterr().err
    assert "HAND_ADDED" in err and "only-here" not in err and "ordo secrets import" in err
    assert "HAND_ADDED=only-here" in (stack / "secrets.env").read_text(encoding="utf-8")


@needs_sops
def test_sops_set_and_rotate_edit_the_sops_file_then_materialize(stack, tmp_path, age_key, monkeypatch):
    target = tmp_path / "ordo.env.sops"
    assert _ordo(stack, "import", "--to", str(target)) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("hf_from_stdin\n"))
    assert _ordo(stack, "set", "HF_TOKEN", "--from-stdin") == 0
    assert secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())["HF_TOKEN"] == "hf_from_stdin"
    assert _values(stack / "secrets.env")["HF_TOKEN"] == "hf_from_stdin"
    before = _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"]
    assert _ordo(stack, "rotate", "OPS_CONTROLLER_TOKEN") == 0
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())["OPS_CONTROLLER_TOKEN"]
    assert stored != before and _values(stack / "secrets.env")["OPS_CONTROLLER_TOKEN"] == stored


@needs_sops
def test_materialize_from_names_the_sops_file_explicitly(stack, tmp_path, age_key):
    target = tmp_path / "elsewhere.env.sops"
    values = {k: v for k, v in _values(stack / "secrets.env").items() if v}
    secret_store.SopsStore(target).write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()), recipients=[secret_store.age_public_key(age_key)])
    assert _ordo(stack, "materialize", "--from", str(target)) == 0


@needs_sops
def test_remote_enable_writes_the_oauth_pair_to_the_sops_store(stack, tmp_path, age_key):
    target = tmp_path / "ordo.env.sops"
    assert _ordo(stack, "import", "--to", str(target)) == 0
    assert cli.main(["remote", "enable", "--out", str(stack), "--yes", "--hostname", "ordo.tail1234.ts.net",
                     "--bind", "100.64.0.1", "--client-id", "cid", "--client-secret", "GOCSPX-x",
                     "--emails", "me@example.com"]) == 0
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    assert stored["OAUTH2_PROXY_CLIENT_SECRET"] == "GOCSPX-x" and stored["OAUTH2_PROXY_COOKIE_SECRET"]
    assert _values(stack / "secrets.env")["OAUTH2_PROXY_CLIENT_SECRET"] == "GOCSPX-x"
    assert cli.main(["remote", "disable", "--out", str(stack), "--yes"]) == 0
    assert "OAUTH2_PROXY_CLIENT_SECRET" not in secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    assert "OAUTH2_PROXY_CLIENT_SECRET" not in _values(stack / "secrets.env")


@needs_sops
def test_init_with_a_secrets_source_writes_generated_secrets_to_sops(tmp_path, age_key, monkeypatch):
    monkeypatch.setattr(wizard, "detect", lambda: HARDWARE)
    monkeypatch.setattr(RENDER_MODULE, "detect", lambda: HARDWARE)
    target = tmp_path / "ordo.env.sops"
    secret_store.SopsStore(target).write_text("LITELLM_SALT_KEY=keep-this-salt\n",
                                              recipients=[secret_store.age_public_key(age_key)])
    out = tmp_path / "out"
    result = wizard.run(CATALOG, REGISTRY, out, interactive=False,
                        answers={"site": {"SECRETS_SOURCE": str(target)}}, host_root=tmp_path / "repo")
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    assert stored["LITELLM_SALT_KEY"] == "keep-this-salt"            # an existing value is never replaced
    assert stored["OPS_CONTROLLER_TOKEN"] and "LITELLM_SALT_KEY" not in result.generated_secret_keys
    assert _values(out / "secrets.env")["OPS_CONTROLLER_TOKEN"] == stored["OPS_CONTROLLER_TOKEN"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_materialized_files_are_owner_only(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "secrets.env").write_text("OPS_CONTROLLER_TOKEN=o\nLITELLM_MASTER_KEY=m\n", encoding="utf-8")
    secret_store.materialize(secret_store.PlainStore(out / "secrets.env"), NEEDS, out)
    assert (out / "secrets" / "discord_token").stat().st_mode & 0o777 == 0o600


@needs_sops
def test_up_materializes_from_the_sops_store(stack, tmp_path, age_key):
    import argparse

    target = tmp_path / "ordo.env.sops"
    assert _ordo(stack, "import", "--to", str(target)) == 0
    stored = secret_store.parse_dotenv(secret_store.SopsStore(target).read_text())
    (stack / "secrets.env").unlink()
    args = argparse.Namespace(source=str(stack / "ordo.yaml"), source_explicit=True, out=str(stack))
    assert cli._prepare_secrets(args, stack) == 0
    materialized = _values(stack / "secrets.env")
    assert list(materialized) == cli._manifest(stack)["required_secrets"]
    assert all(materialized[k] == stored.get(k, "") for k in materialized)
    assert (stack / "secrets" / "discord_token").exists()
