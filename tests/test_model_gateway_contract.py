"""Contract test for Model Gateway API (OpenAI-compatible)."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _load_gateway():
    """Load model-gateway module (folder has hyphen)."""
    gateway_path = Path(__file__).resolve().parent.parent / "model-gateway" / "main.py"
    spec = importlib.util.spec_from_file_location("model_gateway_main", gateway_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mock_llamacpp_models_response():
    """Mock response for llama-server /v1/models."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "object": "list",
        "data": [{"id": "deepseek-r1-7b.Q4_K_M.gguf", "object": "model", "created": 1234567890}],
    }
    resp.raise_for_status = MagicMock()
    return resp


def test_v1_models_returns_openai_format(mock_llamacpp_models_response):
    """GET /v1/models returns OpenAI-compatible list format."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_llamacpp_models_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_client):
        gateway = _load_gateway()
        client = TestClient(gateway.app)
        r = client.get("/v1/models")

    assert r.status_code == 200
    data = r.json()
    assert "object" in data
    assert data["object"] == "list"
    assert "data" in data
    assert isinstance(data["data"], list)
    if data["data"]:
        m = data["data"][0]
        assert "id" in m
        assert "object" in m
        assert m["object"] == "model"
