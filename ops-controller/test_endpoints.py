import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def set_token(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token-for-test")
    import importlib
    import ops_controller.main as m
    importlib.reload(m)
    return m


def test_containers_list_requires_bearer(set_token):
    client = TestClient(set_token.app)
    r = client.get("/containers")
    assert r.status_code in (401, 403)


def test_containers_list_returns_minimal_metadata(set_token):
    client = TestClient(set_token.app)
    r = client.get(
        "/containers", headers={"Authorization": "Bearer test-token-for-test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    if body:
        for entry in body:
            assert set(entry.keys()) >= {"name", "status", "image"}


def test_containers_list_emits_audit_line(set_token, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    import importlib
    import ops_controller.main as m
    importlib.reload(m)
    client = TestClient(m.app)
    client.get("/containers", headers={"Authorization": "Bearer test-token-for-test"})
    audit = (tmp_path / "audit.jsonl").read_text().splitlines()
    import json
    parsed = [json.loads(l) for l in audit]
    assert any(p["action"] == "containers.list" for p in parsed)
