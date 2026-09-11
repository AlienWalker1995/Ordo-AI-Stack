"""bootstrap_keys: desired-state reconciliation of LiteLLM virtual keys (absent -> generate; equal ->
no-op; grants differ -> delete + regenerate, because /key/update MCP grants are not honoured by
tools/list, BerriAI/litellm #35662)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "model-gateway"))

from bootstrap_keys import NO_MCP_SENTINEL, desired_payload, grants_match, reconcile  # noqa: E402

SPEC = [
    {"env": "LITELLM_KEY_HERMES", "alias": "hermes", "models": ["local-chat", "local-embed"],
     "mcp_servers": ["memory-vault", "searxng"]},
    {"env": "LITELLM_KEY_OPEN_WEBUI", "alias": "open-webui", "models": ["local-chat"], "mcp_servers": []},
]
ENV = {"LITELLM_KEY_HERMES": "sk-hermes000000000000000000000000000000",
       "LITELLM_KEY_OPEN_WEBUI": "sk-webui0000000000000000000000000000000"}


class FakeApi:
    def __init__(self, existing=None):
        self.keys = dict(existing or {})   # key value -> info dict
        self.calls = []

    def key_info(self, key):
        self.calls.append(("info", key))
        return self.keys.get(key)

    def generate(self, payload):
        self.calls.append(("generate", payload["key_alias"]))
        self.keys[payload["key"]] = {"key_alias": payload["key_alias"], "models": payload["models"],
                                     "object_permission": payload["object_permission"]}

    def delete(self, key):
        self.calls.append(("delete", key))
        self.keys.pop(key, None)


def test_desired_payload_uses_explicit_key_value_and_no_mcp_sentinel():
    p = desired_payload(SPEC[1], ENV)
    assert p["key"] == ENV["LITELLM_KEY_OPEN_WEBUI"] and p["key_alias"] == "open-webui"
    assert p["object_permission"]["mcp_servers"] == [NO_MCP_SENTINEL]
    assert desired_payload(SPEC[0], ENV)["object_permission"]["mcp_servers"] == ["memory-vault", "searxng"]


def test_desired_payload_fails_loud_on_missing_secret():
    with pytest.raises(ValueError, match="LITELLM_KEY_HERMES"):
        desired_payload(SPEC[0], {"LITELLM_KEY_HERMES": ""})


def test_absent_keys_are_generated():
    api = FakeApi()
    actions = reconcile(SPEC, ENV, api)
    assert actions == ["generated hermes", "generated open-webui"]
    assert [c[0] for c in api.calls] == ["info", "generate", "info", "generate"]


def test_matching_keys_are_untouched():
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    api.calls.clear()
    assert reconcile(SPEC, ENV, api) == ["unchanged hermes", "unchanged open-webui"]
    assert all(c[0] == "info" for c in api.calls)


def test_changed_grants_regenerate_the_key():
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    api.calls.clear()
    changed = [dict(SPEC[0], mcp_servers=["searxng"]), SPEC[1]]
    assert reconcile(changed, ENV, api)[0] == "regenerated hermes"
    assert [c[0] for c in api.calls][:3] == ["info", "delete", "generate"]
    assert api.keys[ENV["LITELLM_KEY_HERMES"]]["object_permission"]["mcp_servers"] == ["searxng"]


def test_grants_match_ignores_order():
    cur = {"key_alias": "a", "models": ["y", "x"], "object_permission": {"mcp_servers": ["b", "a"]}}
    des = {"key_alias": "a", "models": ["x", "y"], "object_permission": {"mcp_servers": ["a", "b"]}}
    assert grants_match(cur, des)
