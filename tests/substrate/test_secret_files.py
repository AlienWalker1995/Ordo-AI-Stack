"""File-delivered secrets: a service that lists a key under `secret_files:` gets a read-only file at
/run/secrets/<key lowercased> and `<ENV>=<that path>`, never the value in its environment.

The mechanism (ordo/render/secret_files.py) is one declaration for every manifest kind; `ordo secrets
materialize` writes the files and a digest per key that the mount's label interpolates, so a rotated
value changes the service's compose config hash like an env secret does.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ordo.host import bringup, secret_store
from ordo.render import secret_files, stack
from ordo.render.agents import Agent
from ordo.render.compose import render_compose
from ordo.render.llamacpp_backend import CPU as CPU_BACKEND
from ordo.render.plugins import McpSpec, Plugin, PluginService

ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out/secrets"


# --------------------------------------------------------------------------- #
# the manifest declaration
# --------------------------------------------------------------------------- #


def test_a_bare_key_is_delivered_as_its_file_convention():
    (ref,) = secret_files.parse_secret_files("svc", ["OPS_CONTROLLER_TOKEN"])
    assert (ref.key, ref.env) == ("OPS_CONTROLLER_TOKEN", "OPS_CONTROLLER_TOKEN_FILE")
    assert ref.file == "ops_controller_token"
    assert ref.target == "/run/secrets/ops_controller_token"
    assert ref.source == f"{SECRETS_DIR}/ops_controller_token"


def test_a_mapping_names_the_variable_the_image_reads():
    (ref,) = secret_files.parse_secret_files("svc", [{"key": "LITELLM_DB_PASSWORD", "env": "POSTGRES_PASSWORD_FILE"}])
    assert (ref.key, ref.env, ref.file) == ("LITELLM_DB_PASSWORD", "POSTGRES_PASSWORD_FILE", "litellm_db_password")


@pytest.mark.parametrize("raw, match", [
    ([{"key": "A", "target": "/x"}], "must be a key NAME"),
    (["lower_case"], "UPPER_SNAKE"),
    ([{"key": "A", "env": "bad-name"}], "UPPER_SNAKE"),
    (["A", "A"], "more than once"),
    ([{"key": "A", "env": "X"}, {"key": "B", "env": "X"}], "used more than once"),
])
def test_malformed_declarations_are_refused(raw, match):
    with pytest.raises(ValueError, match=match):
        secret_files.parse_secret_files("svc", raw)


def test_a_key_is_delivered_one_way_only():
    with pytest.raises(ValueError, match="deliver it one way"):
        secret_files.parse_secret_files("svc", ["A"], env_secrets=["A"])


def test_the_path_variable_cannot_also_be_set_by_hand():
    with pytest.raises(ValueError, match="env block AND by secret_files"):
        secret_files.parse_secret_files("svc", ["A"], explicit_env=["A_FILE"])


def test_every_manifest_kind_parses_the_same_declaration():
    ps = PluginService.from_dict({"name": "x", "image": "x:1", "secret_files": ["TOKEN"]})
    assert ps.secret_files == (secret_files.SecretFileRef("TOKEN", "TOKEN_FILE"),)
    mcp = McpSpec.from_dict({"image": "x:1", "transport": "http", "port": 80, "secret_files": ["TOKEN"]}, "x")
    assert mcp.secret_files == (secret_files.SecretFileRef("TOKEN", "TOKEN_FILE"),)
    agent = Agent.from_dict({"id": "a", "secret_files": [{"key": "DISCORD_BOT_TOKEN", "env": "DISCORD_BOT_TOKEN_FILE"}]})
    assert agent.secret_files == (secret_files.SecretFileRef("DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN_FILE"),)
    with pytest.raises(ValueError, match="deliver it one way"):
        PluginService.from_dict({"name": "x", "image": "x:1", "secrets": ["TOKEN"], "secret_files": ["TOKEN"]})
    with pytest.raises(ValueError, match="env block AND by secret_files"):
        PluginService.from_dict({"name": "x", "image": "x:1", "env": {"TOKEN_FILE": "/x"}, "secret_files": ["TOKEN"]})


# --------------------------------------------------------------------------- #
# the rendered compose
# --------------------------------------------------------------------------- #


def _render_one(service: dict) -> dict:
    plugin = Plugin.from_dict({"id": "p", "kind": "service", "services": [service], "secrets": ["TOKEN", "OTHER"]})
    doc = render_compose(nvidia_gpu=False, llamacpp_backend=CPU_BACKEND, compose_profiles=[],
                         plugin_services=[(plugin, plugin.services[0])])
    return doc["services"][service["name"]]


def test_a_file_secret_renders_as_a_read_only_mount_and_a_path():
    svc = _render_one({"name": "x", "image": "x:1", "secret_files": ["TOKEN"], "volumes": ["data:/data"]})
    assert svc["environment"]["TOKEN_FILE"] == "/run/secrets/token"
    assert f"{SECRETS_DIR}/token:/run/secrets/token:ro" in svc["volumes"]
    assert "data:/data" in svc["volumes"]
    assert svc["labels"]["ordo.secret-file.token"] == "${ORDO_SECRET_FILE_SHA256_TOKEN:-}"


def test_no_file_secret_value_reaches_the_environment():
    svc = _render_one({"name": "x", "image": "x:1", "secrets": ["OTHER"], "secret_files": ["TOKEN"]})
    refs = {m for v in svc["environment"].values() for m in re.findall(r"\$\{([A-Z0-9_]+)", str(v))}
    assert refs == {"OTHER"}


def test_every_file_mount_is_read_only_and_from_the_materialized_dir():
    svc = _render_one({"name": "x", "image": "x:1", "secret_files": ["TOKEN", {"key": "OTHER", "env": "PW_FILE"}]})
    mounts = [v for v in svc["volumes"] if ":/run/secrets/" in v]
    assert mounts == [f"{SECRETS_DIR}/token:/run/secrets/token:ro", f"{SECRETS_DIR}/other:/run/secrets/other:ro"]


def test_the_rendered_compose_says_which_service_reads_which_file():
    doc = {"services": {"a": {"labels": {"ordo.secret-file.token": "x", "ordo.mcp": "true"}},
                        "b": {"labels": {"ordo.secret-file.other_key": "y"}}, "c": {}}}
    assert secret_files.secret_files_in(doc) == [("a", "TOKEN"), ("b", "OTHER_KEY")]


def test_readers_of_a_key_include_the_services_that_mount_it():
    doc = {"services": {
        "env-reader": {"environment": {"TOKEN": "${TOKEN}"}},
        "file-reader": {"labels": {"ordo.secret-file.token": "${ORDO_SECRET_FILE_SHA256_TOKEN:-}"}},
        "other": {"environment": {"TOKEN_OTHER": "${TOKEN_OTHER}"}},
    }}
    assert stack.readers_of(doc, ["TOKEN"]) == ["env-reader", "file-reader"]


def test_every_compose_call_loads_the_digests():
    cmd = bringup.compose_argv("/d", "ordo", "up")
    assert cmd[cmd.index("/d/secrets.env") + 1:cmd.index("up")] == ["--env-file", "/d/secret-files.env"]


# --------------------------------------------------------------------------- #
# materialize
# --------------------------------------------------------------------------- #


NEEDS = secret_store.SecretNeeds(
    required=("OPS_CONTROLLER_TOKEN",),
    files=(secret_store.SecretFile(key="OPS_CONTROLLER_TOKEN", file="ops_controller_token", service="dashboard"),
           secret_store.SecretFile(key="OPS_CONTROLLER_TOKEN", file="ops_controller_token", service="ops-controller"),
           secret_store.SecretFile(key="LITELLM_DB_PASSWORD", file="litellm_db_password", service="litellm-db")),
)


def _plain(out: Path, text: str) -> secret_store.PlainStore:
    out.mkdir(parents=True, exist_ok=True)
    (out / "secrets.env").write_text(text, encoding="utf-8")
    return secret_store.PlainStore(out / "secrets.env")


def test_materialize_writes_every_declared_file_secret_once(tmp_path):
    out = tmp_path / "out"
    store = _plain(out, "OPS_CONTROLLER_TOKEN=tok\nLITELLM_DB_PASSWORD=pw\n")
    result = secret_store.materialize(store, NEEDS, out)
    assert (out / "secrets" / "ops_controller_token").read_text(encoding="utf-8") == "tok"
    assert (out / "secrets" / "litellm_db_password").read_text(encoding="utf-8") == "pw"
    assert sorted(p.name for p in result.files) == ["litellm_db_password", "ops_controller_token"]


def test_materialize_writes_a_digest_per_file_secret_and_no_value(tmp_path):
    out = tmp_path / "out"
    store = _plain(out, "OPS_CONTROLLER_TOKEN=tok\nLITELLM_DB_PASSWORD=pw\n")
    secret_store.materialize(store, NEEDS, out)
    text = (out / "secret-files.env").read_text(encoding="utf-8")
    digests = secret_store.parse_dotenv(text)
    assert digests == {"ORDO_SECRET_FILE_SHA256_LITELLM_DB_PASSWORD": hashlib.sha256(b"pw").hexdigest()[:16],
                       "ORDO_SECRET_FILE_SHA256_OPS_CONTROLLER_TOKEN": hashlib.sha256(b"tok").hexdigest()[:16]}
    assert "tok" not in text.replace(digests["ORDO_SECRET_FILE_SHA256_OPS_CONTROLLER_TOKEN"], "")


def test_a_rotated_value_changes_its_digest(tmp_path):
    out = tmp_path / "out"
    secret_store.materialize(_plain(out, "OPS_CONTROLLER_TOKEN=one\n"), NEEDS, out, strict=False)
    before = (out / "secret-files.env").read_text(encoding="utf-8")
    secret_store.materialize(_plain(out, "OPS_CONTROLLER_TOKEN=two\n"), NEEDS, out, strict=False)
    assert (out / "secret-files.env").read_text(encoding="utf-8") != before


def test_materialize_reports_file_secrets_no_render_declares_and_keeps_them(tmp_path):
    """A running container may still mount a file an older render declared: removing it under the
    mount would break that container's next restart, so materialize names it instead."""
    out = tmp_path / "out"
    (out / "secrets").mkdir(parents=True)
    (out / "secrets" / "discord_token").write_text("old", encoding="utf-8")
    result = secret_store.materialize(_plain(out, "OPS_CONTROLLER_TOKEN=tok\n"), NEEDS, out, strict=False)
    assert [p.name for p in result.stale_files] == ["discord_token"]
    assert (out / "secrets" / "discord_token").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_file_secrets_are_readable_by_the_container_user_inside_an_owner_only_dir(tmp_path):
    """Containers run as their own uid (oauth2-proxy 65532, gpu-gate 10001, ...): the bind-mounted file
    must be readable by it. The host side is protected by the owner-only directory."""
    out = tmp_path / "out"
    secret_store.materialize(_plain(out, "OPS_CONTROLLER_TOKEN=tok\nLITELLM_DB_PASSWORD=pw\n"), NEEDS, out)
    assert (out / "secrets").stat().st_mode & 0o777 == 0o700
    assert (out / "secrets" / "ops_controller_token").stat().st_mode & 0o777 == 0o644
    assert (out / "secret-files.env").stat().st_mode & 0o777 == 0o600


def test_render_creates_an_empty_digests_file_and_never_overwrites_one(tmp_path):
    from ordo.render.catalog import Catalog
    from ordo.render.config import Source
    from ordo.render.engine import render
    from ordo.render.plugins import PluginRegistry

    rc = render(Source.from_dict({"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}}),
                Catalog.load(ROOT / "catalog" / "models.yaml"), PluginRegistry.load(ROOT / "services"))
    out = tmp_path / "out"
    rc.write(out)
    assert (out / "secret-files.env").read_text(encoding="utf-8") == ""
    (out / "secret-files.env").write_text("ORDO_SECRET_FILE_SHA256_X=abc\n", encoding="utf-8")
    rc.write(out)
    assert (out / "secret-files.env").read_text(encoding="utf-8") == "ORDO_SECRET_FILE_SHA256_X=abc\n"


# --------------------------------------------------------------------------- #
# the helpers our own services read secrets with
# --------------------------------------------------------------------------- #


def test_read_secret_prefers_the_file(tmp_path):
    from ordo.secret_env import read_secret

    path = tmp_path / "token"
    path.write_text("from-file\n", encoding="utf-8")
    assert read_secret("TOKEN", {"TOKEN_FILE": str(path)}) == "from-file"
    assert read_secret("TOKEN", {"TOKEN": " from-env "}) == "from-env"
    assert read_secret("TOKEN", {}) == ""


def test_read_secret_fails_loud_on_a_declared_file_that_is_missing(tmp_path):
    from ordo.secret_env import SecretFileError, read_secret

    with pytest.raises(SecretFileError, match="TOKEN_FILE"):
        read_secret("TOKEN", {"TOKEN_FILE": str(tmp_path / "absent")})


def test_read_secret_refuses_two_sources(tmp_path):
    from ordo.secret_env import SecretFileError, read_secret

    path = tmp_path / "token"
    path.write_text("from-file", encoding="utf-8")
    with pytest.raises(SecretFileError, match="both"):
        read_secret("TOKEN", {"TOKEN_FILE": str(path), "TOKEN": "from-env"})


# Every image that reads a secret with the helper carries its own copy (each builds from its own
# context). They must stay byte-identical to the one canonical copy.
PY_HELPER_COPIES = [
    "services/dashboard/dashboard/secret_env.py",
    "services/gpu-gate/secret_env.py",
    "services/comfyui-mcp/secret_env.py",
    "services/orchestration/secret_env.py",
    "services/evals/ordo_evals/secret_env.py",
    "services/model-gateway/secret_env.py",
    "services/langfuse/secret_env.py",
]
SH_HELPER_COPIES = [
    "services/model-gateway/secret-env.sh",
    "services/obsidian-livesync/secret-env.sh",
    "services/n8n/secret-env.sh",
]


@pytest.mark.parametrize("copy", PY_HELPER_COPIES)
def test_python_helper_copies_match_the_canonical_one(copy):
    assert (ROOT / copy).read_bytes() == (ROOT / "ordo" / "secret_env.py").read_bytes()


@pytest.mark.parametrize("copy", SH_HELPER_COPIES)
def test_shell_helper_copies_match_the_canonical_one(copy):
    assert (ROOT / copy).read_bytes() == (ROOT / "ordo" / "secret-env.sh").read_bytes()


def _sh() -> str | None:
    return shutil.which("sh")


@pytest.mark.skipif(_sh() is None, reason="no POSIX sh")
def test_the_shell_helper_exports_each_run_secrets_file(tmp_path):
    secret = tmp_path / "tok"
    secret.write_text("file-value\n", encoding="utf-8")
    script = (f'. "{(ROOT / "ordo" / "secret-env.sh").as_posix()}"\n'
              'ordo_secret_file_env TOKEN\n'
              'printf "%s|%s" "$TOKEN" "${TOKEN_FILE-unset}"\n')
    env = {**os.environ, "TOKEN_FILE": secret.as_posix()}
    env.pop("TOKEN", None)
    out = subprocess.run([_sh(), "-c", script], env=env, capture_output=True, text=True, check=True).stdout
    assert out == "file-value|unset"


@pytest.mark.skipif(_sh() is None, reason="no POSIX sh")
def test_the_shell_helper_fails_loud_on_a_missing_file(tmp_path):
    script = f'. "{(ROOT / "ordo" / "secret-env.sh").as_posix()}"\nordo_secret_file_env TOKEN\necho reached\n'
    env = {**os.environ, "TOKEN_FILE": (tmp_path / "absent").as_posix()}
    proc = subprocess.run([_sh(), "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "reached" not in proc.stdout
    assert "TOKEN_FILE" in proc.stderr


@pytest.mark.skipif(_sh() is None, reason="no POSIX sh")
def test_the_shell_helper_leaves_an_env_value_alone_without_a_file():
    script = f'. "{(ROOT / "ordo" / "secret-env.sh").as_posix()}"\nordo_secret_file_env TOKEN\nprintf "%s" "$TOKEN"\n'
    env = {**os.environ, "TOKEN": "env-value"}
    env.pop("TOKEN_FILE", None)
    out = subprocess.run([_sh(), "-c", script], env=env, capture_output=True, text=True, check=True).stdout
    assert out == "env-value"


def test_an_apply_dry_run_hashes_against_the_digests_out_holds_now(tmp_path):
    """The dry run skips materialize, so it must compare with the digests already in out/: without
    them every file-secret reader would hash differently from the running container."""
    import yaml

    from ordo.host.apply import RealHost

    out = tmp_path / "out"
    out.mkdir()
    (out / "ordo.yaml").write_text(yaml.safe_dump({"hardware": {"gpus": [], "ram_gb": 32, "cpu_cores": 8}}),
                                   encoding="utf-8")
    (out / "secret-files.env").write_text("ORDO_SECRET_FILE_SHA256_OPS_CONTROLLER_TOKEN=abc\n", encoding="utf-8")
    host = RealHost(source_path=out / "ordo.yaml", catalog_path=ROOT / "catalog" / "models.yaml", out=out,
                    project="ordo", preflight=lambda _: True, materialize_secrets=lambda: 0, doctor=lambda: 0)
    with host.staged_render([], dry_run=True) as staged:
        staged_digests = Path(staged.compose_dir) / "secret-files.env"
        assert staged_digests.read_text(encoding="utf-8") == "ORDO_SECRET_FILE_SHA256_OPS_CONTROLLER_TOKEN=abc\n"


class _FakeStatusResponse:
    def __init__(self, request):
        self.request = request

    def read(self):
        return b'{"gpu": {"state": "idle"}}'


@pytest.mark.parametrize("delivery", ["file", "env"])
def test_the_lease_probe_authenticates_from_the_token_file_or_the_env(delivery, tmp_path, monkeypatch, capsys):
    """`ordo up`'s lease probe runs by `docker exec` inside ops-controller, which does not see the
    serving process's environment: with file delivery it must read the file, and it must still work
    against an ops-controller created before file delivery (the env var)."""
    import urllib.request

    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        return _FakeStatusResponse(request)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OPS_CONTROLLER_TOKEN", raising=False)
    monkeypatch.delenv("OPS_CONTROLLER_TOKEN_FILE", raising=False)
    if delivery == "file":
        (tmp_path / "ops_controller_token").write_text("tok-file\n", encoding="utf-8")
        monkeypatch.setenv("OPS_CONTROLLER_TOKEN_FILE", str(tmp_path / "ops_controller_token"))
    else:
        monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "tok-file")
    exec(bringup._STATUS_SCRIPT, {})  # noqa: S102 - the script is ours, run as `docker exec` runs it
    assert seen == ["Bearer tok-file"]
    assert '"state": "idle"' in capsys.readouterr().out


def test_a_hosted_mcp_server_has_nothing_to_mount_a_file_into():
    with pytest.raises(ValueError, match="hosted"):
        McpSpec.from_dict({"url": "https://x.example/mcp", "transport": "http", "secret_files": ["TOKEN"]}, "x")
