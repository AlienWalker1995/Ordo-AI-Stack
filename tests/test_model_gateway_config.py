from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_GATEWAY_DIR = REPO_ROOT / "model-gateway"


def test_litellm_config_exists_and_routes_llamacpp():
    config_text = (MODEL_GATEWAY_DIR / "litellm_config.yaml").read_text(encoding="utf-8")

    assert "model_list:" in config_text
    assert "__CHAT_MODEL__" in config_text
    assert "__EMBED_MODEL__" in config_text
    assert "__MASTER_KEY__" in config_text
    assert 'api_base: "http://llamacpp:8080/v1"' in config_text
    assert 'api_base: "http://llamacpp-embed:8080/v1"' in config_text


def test_litellm_dockerfile_uses_proxy_image():
    dockerfile = (MODEL_GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "ghcr.io/berriai/litellm:main-stable" in dockerfile
    assert "config.template.yaml" in dockerfile
    assert "entrypoint.sh" in dockerfile
