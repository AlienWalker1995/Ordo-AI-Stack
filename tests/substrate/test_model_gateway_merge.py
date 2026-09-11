"""model-gateway entrypoint merge step: the rendered LiteLLM mcp_servers fragment lands in the config
under one key, the master key never lands in the file, and a missing/invalid fragment fails loud."""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "model-gateway"))

from merge_mcp_config import main, merge  # noqa: E402

CONFIG = """\
model_list:
  - model_name: local-chat
    litellm_params: {model: openai/local-chat, api_base: http://llamacpp:8080/v1, api_key: local}
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
FRAGMENT = """\
mcp_servers:
  searxng:
    server_id: searxng
    url: http://mcp-searxng:8080/mcp
    transport: http
"""


def test_merge_adds_mcp_servers_and_keeps_everything_else():
    out = yaml.safe_load(merge(CONFIG, FRAGMENT))
    assert out["mcp_servers"]["searxng"]["url"] == "http://mcp-searxng:8080/mcp"
    assert out["model_list"][0]["model_name"] == "local-chat"
    assert out["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_merge_with_empty_fragment_yields_empty_mapping():
    out = yaml.safe_load(merge(CONFIG, "mcp_servers: {}\n"))
    assert out["mcp_servers"] == {}


def test_merge_rejects_fragment_without_mcp_servers_key():
    with pytest.raises(ValueError):
        merge(CONFIG, "servers: []\n")


def test_merge_rejects_list_shaped_mcp_servers():
    with pytest.raises(ValueError):
        merge(CONFIG, "mcp_servers:\n  - server_name: x\n")


def test_cli_missing_fragment_exits_2(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    assert main([str(cfg), str(tmp_path / "absent.yaml")]) == 2
    assert cfg.read_text() == CONFIG  # untouched


def test_cli_writes_merged_config_in_place(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    frag = tmp_path / "mcp_servers.yaml"
    frag.write_text(FRAGMENT)
    assert main([str(cfg), str(frag)]) == 0
    assert "mcp-searxng" in cfg.read_text()
