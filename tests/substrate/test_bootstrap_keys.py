"""bootstrap_keys: desired-state reconciliation of LiteLLM virtual keys, keyed on the ALIAS.

FakeApi models the real LiteLLM contract, not a convenient one: keys are stored by value with a
token hash, `key_info` reports MCP grants as HASHED server ids (never the names we sent), and
`generate` rejects a duplicate alias exactly like /key/generate does. Those three facts are what
the C1/C2 review findings turned on: comparing hashed ids against declared names made every run
delete and regenerate every key, and a changed key value used to leave the old key live.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "model-gateway"))

from bootstrap_keys import (  # noqa: E402
    NO_MCP_SENTINEL,
    desired_payload,
    grants_match,
    reconcile,
)

SPEC = [
    {"env": "LITELLM_KEY_HERMES", "alias": "hermes", "models": ["local-chat", "local-embed"],
     "mcp_servers": ["memory_vault", "searxng"]},
    {"env": "LITELLM_KEY_OPEN_WEBUI", "alias": "open-webui", "models": ["local-chat"], "mcp_servers": []},
]
ENV = {"LITELLM_KEY_HERMES": "sk-hermes000000000000000000000000000000",
       "LITELLM_KEY_OPEN_WEBUI": "sk-webui0000000000000000000000000000000"}

# What /v1/mcp/server returns: LiteLLM hashes each server name into an opaque server_id and stores
# THAT in object_permission.mcp_servers. The names on the left never appear in a key row.
ID_TO_NAME = {"h_memory_vault": "memory_vault", "h_searxng": "searxng",
              "h_codebase_memory": "codebase_memory"}
NAME_TO_ID = {name: sid for sid, name in ID_TO_NAME.items()}


class FakeApi:
    """LiteLLM's /key/* and /v1/mcp/server surface, as they actually behave."""

    def __init__(self):
        self.rows = {}          # key value -> stored row (as /key/list returns it)
        self.calls = []
        self._next_token = 0

    # -- helpers -------------------------------------------------------------
    def _hash(self, names):
        return [NAME_TO_ID.get(n, n) for n in names]   # the sentinel is not a server: passes through

    # -- KeyApi --------------------------------------------------------------
    def mcp_server_names(self):
        self.calls.append(("mcp_server_names", None))
        return dict(ID_TO_NAME)

    def key_info(self, key):
        self.calls.append(("info", key))
        row = self.rows.get(key)
        if row is None:
            return None
        # /key/info omits `token`; everything else matches the /key/list row.
        return {k: v for k, v in row.items() if k != "token"}

    def keys_by_alias(self, alias):
        self.calls.append(("list", alias))
        return [dict(r) for r in self.rows.values() if r["key_alias"] == alias]

    def generate(self, payload):
        self.calls.append(("generate", payload["key_alias"]))
        if any(r["key_alias"] == payload["key_alias"] for r in self.rows.values()):
            raise RuntimeError(f"Key with alias '{payload['key_alias']}' already exists")
        self._next_token += 1
        self.rows[payload["key"]] = {
            "token": f"tok{self._next_token}",
            "key_name": "sk-..." + payload["key"][-4:],
            "key_alias": payload["key_alias"],
            "models": list(payload["models"]),
            "object_permission": {
                "object_permission_id": "op-1",
                "mcp_servers": self._hash(payload["object_permission"]["mcp_servers"]),
            },
        }

    def delete(self, key):
        self.calls.append(("delete", key))
        for value, row in list(self.rows.items()):
            if value == key or row["token"] == key:
                del self.rows[value]
                return
        raise RuntimeError("/key/delete returned HTTP 404: No keys found")


def _kinds(api):
    return [c[0] for c in api.calls]


# -- desired_payload -----------------------------------------------------------------------------

def test_desired_payload_uses_explicit_key_value_and_no_mcp_sentinel():
    p = desired_payload(SPEC[1], ENV)
    assert p["key"] == ENV["LITELLM_KEY_OPEN_WEBUI"] and p["key_alias"] == "open-webui"
    assert p["object_permission"]["mcp_servers"] == [NO_MCP_SENTINEL]
    assert desired_payload(SPEC[0], ENV)["object_permission"]["mcp_servers"] == ["memory_vault", "searxng"]


def test_desired_payload_fails_loud_on_missing_secret():
    with pytest.raises(ValueError, match="LITELLM_KEY_HERMES"):
        desired_payload(SPEC[0], {"LITELLM_KEY_HERMES": ""})


def test_desired_payload_rejects_an_empty_models_list():
    """LiteLLM reads `models: []` as access to EVERY model, so it can never mean "no models"."""
    with pytest.raises(ValueError, match="no models"):
        desired_payload(dict(SPEC[1], models=[]), ENV)


# -- grants_match --------------------------------------------------------------------------------

def test_grants_match_maps_hashed_ids_back_to_names_and_ignores_order():
    current = {"key_alias": "hermes", "models": ["local-embed", "local-chat"],
               "object_permission": {"mcp_servers": ["h_searxng", "h_memory_vault"]}}
    desired = desired_payload(SPEC[0], ENV)
    assert grants_match(current, desired, ID_TO_NAME)


def test_grants_match_rejects_an_unknown_hashed_id():
    """A grant pointing at a server LiteLLM no longer serves must not read as "up to date"."""
    current = {"key_alias": "hermes", "models": ["local-chat", "local-embed"],
               "object_permission": {"mcp_servers": ["h_memory_vault", "h_retired_server"]}}
    assert not grants_match(current, desired_payload(SPEC[0], ENV), ID_TO_NAME)


def test_grants_match_rejects_a_different_alias_or_model_set():
    desired = desired_payload(SPEC[0], ENV)
    ids = ["h_memory_vault", "h_searxng"]
    assert not grants_match({"key_alias": "other", "models": ["local-chat", "local-embed"],
                             "object_permission": {"mcp_servers": ids}}, desired, ID_TO_NAME)
    assert not grants_match({"key_alias": "hermes", "models": ["local-chat"],
                             "object_permission": {"mcp_servers": ids}}, desired, ID_TO_NAME)


# -- reconcile -----------------------------------------------------------------------------------

def test_absent_keys_are_generated():
    api = FakeApi()
    assert reconcile(SPEC, ENV, api) == ["generated hermes", "generated open-webui"]
    assert _kinds(api) == ["mcp_server_names", "list", "info", "generate",
                           "list", "info", "generate"]


def test_second_run_is_unchanged_even_though_grants_come_back_hashed():
    """The C1 bug: hashed ids compared against declared names churned every key on every boot."""
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    tokens_before = {v["key_alias"]: v["token"] for v in api.rows.values()}
    api.calls.clear()
    assert reconcile(SPEC, ENV, api) == ["unchanged hermes", "unchanged open-webui"]
    assert "generate" not in _kinds(api) and "delete" not in _kinds(api)
    assert {v["key_alias"]: v["token"] for v in api.rows.values()} == tokens_before


def test_sentinel_only_key_is_unchanged_on_a_second_run():
    api = FakeApi()
    reconcile([SPEC[1]], ENV, api)
    row = next(iter(api.rows.values()))
    assert row["object_permission"]["mcp_servers"] == [NO_MCP_SENTINEL]
    assert reconcile([SPEC[1]], ENV, api) == ["unchanged open-webui"]


def test_a_new_value_rotates_the_alias_and_revokes_the_old_key():
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    old_value = ENV["LITELLM_KEY_HERMES"]
    old_token = api.rows[old_value]["token"]
    rotated_env = dict(ENV, LITELLM_KEY_HERMES="sk-hermesNEW0000000000000000000000000")
    api.calls.clear()

    assert reconcile(SPEC, rotated_env, api)[0] == "rotated hermes"

    assert ("delete", old_token) in api.calls, "the old key must be deleted by its token hash"
    assert old_value not in api.rows, "rotation must revoke the previous key, not leave it live"
    assert api.rows[rotated_env["LITELLM_KEY_HERMES"]]["key_alias"] == "hermes"


def test_grant_drift_regenerates_the_same_value():
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    api.calls.clear()
    drifted = [dict(SPEC[0], mcp_servers=["searxng"]), SPEC[1]]

    assert reconcile(drifted, ENV, api)[0] == "regenerated hermes"

    assert _kinds(api)[:4] == ["mcp_server_names", "list", "info", "delete"]
    assert api.rows[ENV["LITELLM_KEY_HERMES"]]["object_permission"]["mcp_servers"] == ["h_searxng"]


def test_a_value_shared_with_another_alias_raises_instead_of_deleting_it():
    api = FakeApi()
    reconcile([SPEC[0]], ENV, api)
    collided = [dict(SPEC[1], env="LITELLM_KEY_HERMES")]   # open-webui pointed at hermes' value

    with pytest.raises(RuntimeError, match="hermes"):
        reconcile(collided, ENV, api)

    assert api.rows[ENV["LITELLM_KEY_HERMES"]]["key_alias"] == "hermes", "hermes' key must survive"


def test_mcp_server_names_is_fetched_once_per_reconcile():
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    assert _kinds(api).count("mcp_server_names") == 1


def test_unchanged_branch_revokes_a_stale_duplicate_of_the_alias():
    """A second key carrying our alias (made by hand in the LiteLLM UI) is a stale credential:
    the alias is the identity, so the unchanged branch revokes it and keeps our key."""
    api = FakeApi()
    reconcile(SPEC, ENV, api)
    ours = api.rows[ENV["LITELLM_KEY_HERMES"]]
    api.rows["sk-byhand0000000000000000000000000000000"] = dict(
        ours, token="tok-stale", key_name="sk-...hand")
    api.calls.clear()

    actions = reconcile(SPEC, ENV, api)

    assert "revoked a stale duplicate of hermes" in actions and "unchanged hermes" in actions
    assert ("delete", "tok-stale") in api.calls
    assert ENV["LITELLM_KEY_HERMES"] in api.rows, "our own key must survive"
    assert "sk-byhand0000000000000000000000000000000" not in api.rows
    assert "generate" not in _kinds(api)


def test_value_row_missing_from_the_alias_listing_is_deleted_by_value():
    """The /key/info row can exist while /key/list does not return it (listing lag or a page
    boundary): the delete-by-value branch must then revoke it before generate, or generate
    would fail on the alias."""
    class LaggingList(FakeApi):
        def keys_by_alias(self, alias):
            self.calls.append(("list", alias))
            return []

    api = LaggingList()
    reconcile(SPEC, ENV, api)
    drifted = [dict(SPEC[0], models=["local-chat"]), *SPEC[1:]]
    api.calls.clear()

    actions = reconcile(drifted, ENV, api)

    assert actions[0] == "regenerated hermes"
    assert ("delete", ENV["LITELLM_KEY_HERMES"]) in api.calls
    assert api.rows[ENV["LITELLM_KEY_HERMES"]]["models"] == ["local-chat"]


def test_on_action_reports_each_action_as_it_completes():
    api = FakeApi()
    seen = []
    reconcile(SPEC, ENV, api, on_action=seen.append)
    assert seen == [f"generated {e['alias']}" for e in SPEC]


def test_key_info_treats_only_404_as_absent(monkeypatch):
    from bootstrap_keys import HttpKeyApi
    api = HttpKeyApi("http://gateway", "sk-master")
    monkeypatch.setattr(api, "_request", lambda *a, **k: (404, {"error": "not found"}))
    assert api.key_info("sk-x") is None
    monkeypatch.setattr(api, "_request", lambda *a, **k: (400, {"error": "bad request"}))
    with pytest.raises(RuntimeError):
        api.key_info("sk-x")
