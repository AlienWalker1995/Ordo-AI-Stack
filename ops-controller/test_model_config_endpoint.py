"""Endpoint tests for GET/POST /model-config (the dashboard control-plane API).

Uses temp .env + registry + models dir, reloads main against them, and stubs
_recreate_service so no docker runs.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# === MODEL CONFIGS ===\n"
        "LLAMACPP_MODEL=base.gguf\n"
        "LLAMACPP_CTX_SIZE=262144\n"
        "LLAMACPP_ROPE_SCALING=none\n"
        "LLAMACPP_EXTRA_ARGS=--reasoning-format deepseek\n"
        "# LLAMACPP_MODEL=preset-a3b.gguf\n",  # commented preset MUST survive edits
        encoding="utf-8",
    )
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 1, "models": {
        "local-chat": {"id": "local-chat", "kind": "chat", "service": "llamacpp",
                       "runtime": "single-model", "enabled": True,
                       "source": {"file": "base.gguf"}, "config": {},
                       "gpu_uuid": None, "est_vram_gb": 0.0,
                       "updated_by": "test", "updated_at": None}}}), encoding="utf-8")
    models = tmp_path / "gguf"
    models.mkdir()
    (models / "base.gguf").write_bytes(b"x")
    (models / "dense27b.gguf").write_bytes(b"x")

    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", TOKEN)
    monkeypatch.setenv("OPS_ENV_PATH", str(env))
    monkeypatch.setenv("MODEL_REGISTRY_PATH", str(reg))
    monkeypatch.setenv("LLAMACPP_MODELS_DIR", str(models))

    import ops_controller.main as m
    importlib.reload(m)
    calls = []
    monkeypatch.setattr(m, "_recreate_service",
                        lambda svc, request=None: (calls.append(svc), {"ok": True, "service": svc})[1])
    return m, env, reg, calls


def test_get_model_config(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).get("/model-config", headers=AUTH)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["active_model"] == "base.gguf"
    assert any(d["key"] == "LLAMACPP_CTX_SIZE" for d in b["flags"])
    assert "base.gguf" in b["models"] and "dense27b.gguf" in b["models"]
    assert b["effective"]["LLAMACPP_CTX_SIZE"] == "262144"


def test_get_requires_auth(app_env):
    m, env, reg, calls = app_env
    assert TestClient(m.app).get("/model-config").status_code in (401, 403)


def test_post_sets_override_and_recreates(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).post("/model-config", headers=AUTH,
                               json={"confirm": True, "overrides": {"LLAMACPP_CTX_SIZE": "524288"}})
    assert r.status_code == 200, r.text
    txt = env.read_text()
    assert "LLAMACPP_CTX_SIZE=524288" in txt
    assert "# LLAMACPP_MODEL=preset-a3b.gguf" in txt  # commented preset preserved
    assert "llamacpp" in calls
    cfg = json.loads(reg.read_text())["models"]["local-chat"]["config"]
    assert cfg.get("LLAMACPP_CTX_SIZE") == "524288"


def test_post_validation_400_no_recreate(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).post("/model-config", headers=AUTH,
                               json={"confirm": True, "overrides": {"LLAMACPP_ROPE_SCALING": "bogus"}})
    assert r.status_code == 400
    assert "LLAMACPP_ROPE_SCALING" in r.text
    assert calls == []


def test_post_requires_confirm(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).post("/model-config", headers=AUTH,
                               json={"overrides": {"LLAMACPP_CTX_SIZE": "524288"}})
    assert r.status_code == 400


def test_post_clear_reverts_to_default(app_env):
    m, env, reg, calls = app_env
    c = TestClient(m.app)
    c.post("/model-config", headers=AUTH, json={"confirm": True, "overrides": {"LLAMACPP_CTX_SIZE": "524288"}})
    c.post("/model-config", headers=AUTH, json={"confirm": True, "overrides": {"LLAMACPP_CTX_SIZE": None}})
    assert "LLAMACPP_CTX_SIZE=262144" in env.read_text()  # back to default baseline


def test_post_model_swap_updates_source(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).post("/model-config", headers=AUTH,
                               json={"confirm": True, "overrides": {"LLAMACPP_MODEL": "dense27b.gguf"}})
    assert r.status_code == 200, r.text
    assert "LLAMACPP_MODEL=dense27b.gguf" in env.read_text()
    assert json.loads(reg.read_text())["models"]["local-chat"]["source"]["file"] == "dense27b.gguf"


def test_post_rejects_missing_model_file(app_env):
    m, env, reg, calls = app_env
    r = TestClient(m.app).post("/model-config", headers=AUTH,
                               json={"confirm": True, "overrides": {"LLAMACPP_MODEL": "nope.gguf"}})
    assert r.status_code == 400
    assert calls == []
