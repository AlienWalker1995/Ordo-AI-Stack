#!/usr/bin/env python3
"""Idempotent LiteLLM virtual-key provisioning from the render-emitted keys.json.

Runs as the one-shot `model-gateway-keys` compose service after model-gateway is healthy.
For every entry in /config/keys.json ({"env", "alias", "models", "mcp_servers"}) the key VALUE
comes from the environment variable named by `env` (secrets.env).

The reconcile is keyed on the ALIAS, not on the key value, because the alias is the stable
identity of a consumer and the value is what rotates. Per entry we look the alias up with
/key/list and the value up with /key/info, then:

  by_value present, same alias, grants equal   -> `unchanged` (nothing is touched)
  by_value present, grants drifted             -> delete + regenerate -> `regenerated`
  by_value absent, alias already has key(s)    -> delete those + generate -> `rotated`
                                                  (the OLD key is revoked; that is the point)
  neither present                              -> generate -> `generated`
  by_value present under a DIFFERENT alias     -> RuntimeError (a value collision between two
                                                  consumers; never delete another consumer's key)

Grants are compared after mapping LiteLLM's stored `object_permission.mcp_servers` (HASHED
server ids) back to server NAMES via /v1/mcp/server, which is fetched once per run. Without that
mapping every run saw "hashed id != declared name", deleted the key and generated a new one, so
every consumer's key churned on every boot (and any consumer still holding the previous value was
stranded). Regeneration rather than /key/update is deliberate: MCP grants applied via /key/update
are not visible to tools/list (BerriAI/litellm #35662). A regenerated key's spend history resets.

Exit 0 when the desired state holds for every key, 1 otherwise (the agent depends on this).
Stdlib only (urllib) so the substrate tests import it without LiteLLM/httpx.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

# LiteLLM's sentinel: a key whose mcp_servers is exactly this list can reach NO MCP server, even
# if a team/org grant would otherwise apply. Used for keys that declare `mcp_servers: []`.
NO_MCP_SENTINEL = "no-mcp-servers"

# /key/list page size. An exact-alias listing returns at most a handful of rows; we still follow
# total_pages so a delete can never miss a key that carries the alias.
KEY_LIST_PAGE_SIZE = 100


class KeyApi(Protocol):
    def key_info(self, key: str) -> dict[str, Any] | None: ...
    def keys_by_alias(self, alias: str) -> list[dict[str, Any]]: ...
    def mcp_server_names(self) -> dict[str, str]: ...
    def generate(self, payload: dict[str, Any]) -> None: ...
    def delete(self, key: str) -> None: ...


class HttpKeyApi:
    """The real /key/* client (master key auth)."""

    def __init__(self, base_url: str, master_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"detail": raw}

    def key_info(self, key: str) -> dict[str, Any] | None:
        # The key VALUE travels in the query string because LiteLLM offers no POST form of
        # /key/info. Consequence: never raise LITELLM_LOG above ERROR in production, since DEBUG
        # logs the request line and the request line here carries a live key.
        status, data = self._request("GET", "/key/info?key=" + urllib.parse.quote(key, safe=""))
        if status == 200:
            return dict(data.get("info") or {})
        if status == 404:   # the only "absent" signal; a 400 is a contract change and must be loud
            return None
        raise RuntimeError(f"/key/info returned HTTP {status}: {data}")

    def keys_by_alias(self, alias: str) -> list[dict[str, Any]]:
        """Every key carrying `alias`, as full objects (they carry `token`, the delete handle)."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({
                "key_alias": alias, "return_full_object": "true",
                "size": KEY_LIST_PAGE_SIZE, "page": page,
            })
            status, data = self._request("GET", "/key/list?" + query)
            if status != 200:
                raise RuntimeError(f"/key/list for alias {alias!r} returned HTTP {status}: {data}")
            batch = [dict(k) for k in (data.get("keys") or []) if isinstance(k, dict)]
            # LiteLLM matches key_alias exactly; re-filtering here is belt and braces so a future
            # prefix/contains match could never make us delete a different consumer's key.
            rows += [k for k in batch if k.get("key_alias") == alias]
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages or not batch:
                return rows
            page += 1

    def mcp_server_names(self) -> dict[str, str]:
        """{server_id (hashed) -> server_name}, the map that makes stored grants comparable."""
        status, data = self._request("GET", "/v1/mcp/server")
        if status != 200:
            raise RuntimeError(f"/v1/mcp/server returned HTTP {status}: {data}")
        rows = data if isinstance(data, list) else (data.get("data") or [])
        return {str(r["server_id"]): str(r.get("server_name") or "")
                for r in rows if isinstance(r, dict) and r.get("server_id")}

    def generate(self, payload: dict[str, Any]) -> None:
        status, data = self._request("POST", "/key/generate", payload)
        if status != 200:
            raise RuntimeError(f"/key/generate for {payload.get('key_alias')} returned HTTP {status}: {data}")

    def delete(self, key: str) -> None:
        status, data = self._request("POST", "/key/delete", {"keys": [key]})
        if status != 200:
            raise RuntimeError(f"/key/delete returned HTTP {status}: {data}")


def desired_payload(entry: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    value = str(env.get(entry["env"], "") or "").strip()
    if not value:
        raise ValueError(f"{entry['env']} is empty or unset (fill it in secrets.env; the wizard generates it)")
    models = [str(m) for m in (entry.get("models") or [])]
    # Belt and braces with ordo.render.render_litellm_keys: LiteLLM reads `models: []` as ALL
    # models, so an empty list is a silent privilege escalation, never an empty grant.
    if not models:
        raise ValueError(f"litellm key '{entry['alias']}' declares no models (LiteLLM reads an empty "
                         "list as access to EVERY model; name the models explicitly)")
    granted = [str(s) for s in (entry.get("mcp_servers") or [])] or [NO_MCP_SENTINEL]
    return {
        "key": value,
        "key_alias": str(entry["alias"]),
        "models": models,
        "object_permission": {"mcp_servers": granted},
    }


def grants_match(current: dict[str, Any], desired: dict[str, Any], id_to_name: Mapping[str, str]) -> bool:
    """True when the LIVE key already carries exactly the desired alias, models and MCP grants.

    `current` is a /key/info (or /key/list) row, so its mcp_servers are HASHED server ids; they are
    mapped through `id_to_name` before the comparison. An id absent from the map is a mismatch (the
    grant points at a server LiteLLM no longer serves). The `no-mcp-servers` sentinel is not a
    server id and maps to itself. Pure function: no I/O, so the tests can pin the semantics.
    """
    if current.get("key_alias") != desired["key_alias"]:
        return False
    if sorted(str(m) for m in (current.get("models") or [])) != sorted(desired["models"]):
        return False
    current_names: list[str] = []
    for raw_id in ((current.get("object_permission") or {}).get("mcp_servers") or []):
        server_id = str(raw_id)
        if server_id == NO_MCP_SENTINEL:
            current_names.append(server_id)
            continue
        name = id_to_name.get(server_id)
        if not name:
            return False
        current_names.append(name)
    return sorted(current_names) == sorted(desired["object_permission"]["mcp_servers"])


def _covered_by_alias_rows(info: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    """True when the /key/info row is one of the /key/list rows for the same alias.

    /key/info omits `token`, so the masked `key_name` is the only identifier both endpoints
    return. Deleting the same key twice is a hard 404 from /key/delete, so the delete-by-value is
    skipped whenever the alias listing already covers that key.
    """
    key_name = info.get("key_name")
    return bool(key_name) and any(r.get("key_name") == key_name for r in rows)


def reconcile(
    spec: list[dict[str, Any]],
    env: Mapping[str, str],
    api: KeyApi,
    on_action: Callable[[str], None] | None = None,
) -> list[str]:
    """Drive every consumer to its desired key. `on_action` (when given) is called with each action
    the moment it is complete, so a failure later in the run still leaves a trail of what changed."""
    actions: list[str] = []

    def record(action: str) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    id_to_name = api.mcp_server_names()   # once per run: the map is stack-wide, not per key
    for entry in spec:
        desired = desired_payload(entry, env)
        alias = desired["key_alias"]
        by_alias = api.keys_by_alias(alias)
        by_value = api.key_info(desired["key"])
        if by_value is not None and by_value.get("key_alias") != alias:
            raise RuntimeError(
                f"the key value for alias '{alias}' is already registered under alias "
                f"'{by_value.get('key_alias')}': two consumers share one secret. Give '{alias}' its "
                "own value in secrets.env (refusing to delete another consumer's key)")
        if by_value is not None and grants_match(by_value, desired, id_to_name):
            # The alias is the identity: any OTHER key carrying it (created by hand in the LiteLLM UI
            # or by a manual /key/generate) is a stale credential and is revoked here.
            for row in by_alias:
                if row.get("token") and row.get("key_name") != by_value.get("key_name"):
                    api.delete(str(row["token"]))
                    record(f"revoked a stale duplicate of {alias}")
            record(f"unchanged {alias}")
            continue
        for token in [str(row["token"]) for row in by_alias if row.get("token")]:
            api.delete(token)
        if by_value is not None and not _covered_by_alias_rows(by_value, by_alias):
            api.delete(desired["key"])
        api.generate(desired)
        if by_value is not None:
            record(f"regenerated {alias}")     # same value, grants had drifted
        elif by_alias:
            record(f"rotated {alias}")         # new value: the old key is now revoked
        else:
            record(f"generated {alias}")
    return actions


def main() -> int:
    base_url = os.environ.get("MODEL_GATEWAY_URL", "http://model-gateway:11435")
    spec_path = os.environ.get("LITELLM_KEYS_SPEC", "/config/keys.json")
    master = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master:
        print("bootstrap_keys: LITELLM_MASTER_KEY is unset", file=sys.stderr)
        return 1
    try:
        with open(spec_path, encoding="utf-8") as fh:
            spec = json.load(fh)["keys"]
    except (OSError, KeyError, ValueError) as e:
        print(f"bootstrap_keys: cannot read {spec_path}: {e} (re-run `ordo render`)", file=sys.stderr)
        return 1
    try:
        reconcile(spec, os.environ, HttpKeyApi(base_url, master),
                  on_action=lambda action: print(f"bootstrap_keys: {action}", flush=True))
    except Exception as e:  # noqa: BLE001 - any failure is a non-zero exit for the depends_on gate
        print(f"bootstrap_keys: FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
