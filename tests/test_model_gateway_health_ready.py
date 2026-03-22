"""Tests for model-gateway /health and /ready (M7)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def _load_gateway():
    gateway_path = Path(__file__).resolve().parent.parent / "model-gateway" / "main.py"
    spec = importlib.util.spec_from_file_location("model_gateway_health", gateway_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_client_for_health_ready(tags_models: list | None = None):
    """AsyncClient mock: /api/version, /api/tags, optional vLLM."""
    tags_models = tags_models if tags_models is not None else [{"name": "m:latest"}]

    async def get_side_effect(url: str, **kwargs):
        r = MagicMock()
        r.headers = {"content-type": "application/json"}
        if url.endswith("/api/version"):
            r.status_code = 200
            r.json.return_value = {"version": "0.5.0"}
        elif url.endswith("/api/tags"):
            r.status_code = 200
            r.json.return_value = {"models": tags_models}
        elif "/v1/models" in url:
            r.status_code = 200
            r.json.return_value = {"data": []}
        elif "/health" in url and "vllm" in url.lower():
            r.status_code = 200
            r.json.return_value = {}
        else:
            r.status_code = 404
            r.json.return_value = {}
        return r

    mock = AsyncMock()
    mock.get = AsyncMock(side_effect=get_side_effect)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def test_health_returns_gateway_shape():
    """GET /health returns ok, service, providers, model_cache."""
    gateway = _load_gateway()
    with patch.dict(os.environ, {"VLLM_URL": ""}, clear=False):
        with patch.object(gateway, "AsyncClient", return_value=_mock_client_for_health_ready()):
            client = TestClient(gateway.app)
            r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("service") == "model-gateway"
    assert "ok" in data
    assert "providers" in data
    assert "ollama" in data["providers"]
    assert "vllm" in data["providers"]
    assert data["providers"]["vllm"].get("skipped") is True
    assert "model_cache" in data


def test_ready_returns_200_when_models_listed():
    """GET /ready returns 200 and ready=true when at least one model exists."""
    gateway = _load_gateway()
    with patch.dict(os.environ, {"VLLM_URL": ""}, clear=False):
        with patch.object(gateway, "AsyncClient", return_value=_mock_client_for_health_ready()):
            client = TestClient(gateway.app)
            r = client.get("/ready")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ready") is True
    assert data.get("level") == "l2"
    assert data.get("degraded") is False
    assert data.get("model_count", 0) >= 1


def test_ready_returns_503_when_no_models():
    """GET /ready returns 503 when Ollama is up but model list is empty."""
    gateway = _load_gateway()
    with patch.dict(os.environ, {"VLLM_URL": ""}, clear=False):
        with patch.object(gateway, "AsyncClient", return_value=_mock_client_for_health_ready(tags_models=[])):
            client = TestClient(gateway.app)
            r = client.get("/ready")
    assert r.status_code == 503
    data = r.json()
    assert data.get("ready") is False
    assert data.get("degraded") is True
    assert data.get("reason") == "no_models_configured"
