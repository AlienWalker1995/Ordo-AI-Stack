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


def test_openclaw_trim_messages_to_budget():
    """_trim_messages_to_budget reduces messages when they exceed the budget."""
    gateway = _load_gateway()
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "x " * 5000},
        {"role": "assistant", "content": "Sure, here is a response."},
        {"role": "user", "content": "Follow up question."},
    ]
    trimmed, stats = gateway._trim_messages_to_budget(messages, 512)
    assert stats["after"] <= 512
    assert stats["after"] < stats["before"]
    assert stats["before"] > 512


def _chat_completion_client(payload: dict):
    """httpx.AsyncClient mock for a non-streaming chat completion response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()

    mock = AsyncMock()
    mock.post = AsyncMock(return_value=resp)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def test_chat_completions_strips_reasoning_and_agent_wrappers():
    """POST /v1/chat/completions returns plain assistant text for fragile clients."""
    gateway = _load_gateway()
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "model.gguf",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": (
                    "<thinking>internal chain</thinking>\n"
                    "<attempt_completion><result>Hello from the model.</result></attempt_completion>"
                ),
                "reasoning_content": "internal chain",
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    with patch.object(gateway, "AsyncClient", return_value=_chat_completion_client(payload)):
        client = TestClient(gateway.app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "model.gguf", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert r.status_code == 200
    data = r.json()
    message = data["choices"][0]["message"]
    assert message["content"] == "Hello from the model."
    assert "reasoning_content" not in message


def _reasoning_streaming_client():
    """Mock backend chat completion used by the normalized restreaming path."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "model.gguf",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "Hello",
                "reasoning_content": "internal",
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    fake_resp.raise_for_status = MagicMock()

    mock = AsyncMock()
    mock.post = AsyncMock(return_value=fake_resp)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def test_streaming_chat_completions_omit_reasoning_only_chunks():
    """Streaming /v1/chat/completions drops reasoning-only deltas for strict clients."""
    gateway = _load_gateway()

    with patch.object(gateway, "AsyncClient", return_value=_reasoning_streaming_client()):
        client = TestClient(gateway.app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "model.gguf", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )

    assert r.status_code == 200
    body = r.text
    assert "reasoning_content" not in body
    assert '"content": "Hello"' in body or '"content":"Hello"' in body


def test_cline_requests_preserve_attempt_completion_markup():
    """Cline-compatible requests keep XML control tags while dropping reasoning noise."""
    gateway = _load_gateway()
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "model.gguf",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": (
                    "<thinking>internal chain</thinking>\n"
                    "<attempt_completion><result>Hello from the model.</result></attempt_completion>"
                ),
                "reasoning_content": "internal chain",
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    with patch.object(gateway, "AsyncClient", return_value=_chat_completion_client(payload)):
        client = TestClient(gateway.app)
        r = client.post(
            "/v1/chat/completions",
            json={"model": "model.gguf", "messages": [{"role": "user", "content": "hi"}]},
            headers={"user-agent": "OpenAI/JS 4.96.0", "x-stainless-lang": "js"},
        )

    assert r.status_code == 200
    data = r.json()
    message = data["choices"][0]["message"]
    assert "<attempt_completion>" in message["content"]
    assert "reasoning_content" not in message
