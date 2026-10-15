"""`github-app-token` (services/hermes/github-app-token): Hermes's GitHub App identity.

It must read the app ID and key only from their file secrets, refuse clearly (exit 78 with the setup
steps) when they are missing, reject a key that is not a one-line base64 PEM, sign a JWT GitHub
accepts (RS256, iss = the app ID, a short lifetime), and ask for a token scoped to the one repository.
"""
from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "services" / "hermes" / "github-app-token"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
# Signing hands the key to openssl as /dev/fd/N (pass_fds): Linux, where the helper runs.
linux_only = pytest.mark.skipif(sys.platform == "win32", reason="the helper runs in the Linux agent image")


def load_helper():
    loader = importlib.machinery.SourceFileLoader("github_app_token", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def app_key(tmp_path):
    key = tmp_path / "key.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    public = tmp_path / "public.pem"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True,
                   capture_output=True)
    return key, public


def secrets(tmp_path, monkeypatch, app_id: str, key_value: str):
    (tmp_path / "app_id").write_text(app_id)
    (tmp_path / "app_key").write_text(key_value)
    monkeypatch.setenv("GITHUB_APP_ID_FILE", str(tmp_path / "app_id"))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(tmp_path / "app_key"))


def unpad(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_without_the_secrets_it_exits_78_with_the_setup_steps(tmp_path, monkeypatch, capsys):
    helper = load_helper()
    monkeypatch.setenv("GITHUB_APP_ID_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(tmp_path / "absent"))
    monkeypatch.setattr("sys.argv", ["github-app-token", "owner/repo"])
    assert helper.main() == 78
    err = capsys.readouterr().err
    assert "ordo secrets set GITHUB_APP_ID" in err and "base64 -w0" in err


def test_a_key_that_is_not_one_line_base64_is_refused(tmp_path, monkeypatch):
    helper = load_helper()
    secrets(tmp_path, monkeypatch, "123", "-----BEGIN RSA PRIVATE KEY----- not base64")
    monkeypatch.setattr("sys.argv", ["github-app-token", "owner/repo"])
    with pytest.raises(SystemExit, match="base64-encoded on one line"):
        helper.main()


@linux_only
def test_the_jwt_is_rs256_for_the_app_and_verifies_with_its_public_key(app_key, tmp_path):
    helper = load_helper()
    key, public = app_key
    jwt = helper.app_jwt("5330763", key.read_bytes())
    header, claims, signature = jwt.split(".")
    assert json.loads(unpad(header)) == {"alg": "RS256", "typ": "JWT"}
    body = json.loads(unpad(claims))
    assert body["iss"] == "5330763" and 0 < body["exp"] - body["iat"] <= 600
    (tmp_path / "signed").write_bytes(f"{header}.{claims}".encode())
    (tmp_path / "signature").write_bytes(unpad(signature))
    verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature",
                               str(tmp_path / "signature"), str(tmp_path / "signed")], capture_output=True)
    assert verified.returncode == 0, verified.stderr


@linux_only
def test_it_asks_for_a_token_scoped_to_the_one_repository(app_key, tmp_path, monkeypatch, capsys):
    helper = load_helper()
    key, _ = app_key
    secrets(tmp_path, monkeypatch, "5330763", base64.b64encode(key.read_bytes()).decode())
    calls = []

    def fake_call(method, path, bearer, body=None):
        calls.append((method, path, body))
        if path == "/repos/owner/repo/installation":
            return {"id": 42}
        return {"token": "ghs_example"}

    monkeypatch.setattr(helper, "call", fake_call)
    monkeypatch.setattr("sys.argv", ["github-app-token", "owner/repo"])
    assert helper.main() == 0
    assert capsys.readouterr().out.strip() == "ghs_example"
    assert calls == [("GET", "/repos/owner/repo/installation", None),
                     ("POST", "/app/installations/42/access_tokens", {"repositories": ["repo"]})]


def test_the_image_installs_it_and_never_bridges_its_secrets_into_the_environment():
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY github-app-token /usr/local/bin/github-app-token" in dockerfile
    init = (ROOT / "services" / "hermes" / "ordo-init.sh").read_text(encoding="utf-8")
    assert "GITHUB_APP_PRIVATE_KEY" not in init and "GITHUB_APP_ID" not in init
