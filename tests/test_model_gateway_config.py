from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_GATEWAY_DIR = REPO_ROOT / "model-gateway"


def test_litellm_config_advertises_canonical_model_names():
    config_text = (MODEL_GATEWAY_DIR / "litellm_config.yaml").read_text(encoding="utf-8")

    # Stable identities — never change with GGUF swaps.
    assert 'model_name: "local-chat"' in config_text
    assert 'model_name: "local-embed"' in config_text

    # The 'model' field is what LiteLLM forwards upstream — verify it matches the canonical name.
    assert 'model: "openai/local-chat"' in config_text
    assert 'model: "openai/local-embed"' in config_text

    # Underlying api_base routing preserved.
    assert 'api_base: "http://llamacpp:8080/v1"' in config_text
    assert 'api_base: "http://llamacpp-embed:8080/v1"' in config_text

    # Master key and context size still templated for entrypoint substitution.
    assert "__MASTER_KEY__" in config_text
    assert "__CTX_SIZE__" in config_text

    # model_info advertises context window from LLAMACPP_CTX_SIZE.
    assert "max_input_tokens: __CTX_SIZE__" in config_text

    # Old templated GGUF placeholders are gone.
    assert "__CHAT_MODEL__" not in config_text
    assert "__EMBED_MODEL__" not in config_text


def test_litellm_dockerfile_uses_proxy_image():
    dockerfile = (MODEL_GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "ghcr.io/berriai/litellm:" in dockerfile
    assert "config.template.yaml" in dockerfile
    assert "entrypoint.sh" in dockerfile
