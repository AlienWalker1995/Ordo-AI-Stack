#!/usr/bin/env python3
"""Idempotent LiteLLM virtual-key provisioning from the render-emitted keys.json.

Runs as the one-shot `model-gateway-keys` compose service after model-gateway is healthy.
For every entry in /config/keys.json ({"env", "alias", "models", "mcp_servers"}) the key VALUE
comes from the environment variable named by `env` (secrets.env). Desired-state loop:
  absent           -> POST /key/generate with the explicit key value + grants
  present, equal   -> nothing
  present, differs -> POST /key/delete then /key/generate (LiteLLM #35662: MCP grants applied via
                      /key/update are not visible to tools/list; regenerating is the reliable path;
                      that key's spend history resets)
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
from collections.abc import Mapping
from typing import Any, Protocol

# LiteLLM's sentinel: a key whose mcp_servers is exactly this list can reach NO MCP server, even
# if a team/org grant would otherwise apply. Used for keys that declare `mcp_servers: []`.
NO_MCP_SENTINEL = "no-mcp-servers"


class KeyApi(Protocol):
    def key_info(self, key: str) -> dict[str, Any] | None: ...
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
        status, data = self._request("GET", "/key/info?key=" + urllib.parse.quote(key, safe=""))
        if status == 200:
            return dict(data.get("info") or {})
        if status in (400, 404):
            return None
        raise RuntimeError(f"/key/info returned HTTP {status}: {data}")

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
    granted = [str(s) for s in (entry.get("mcp_servers") or [])] or [NO_MCP_SENTINEL]
    return {
        "key": value,
        "key_alias": str(entry["alias"]),
        "models": [str(m) for m in (entry.get("models") or [])],
        "object_permission": {"mcp_servers": granted},
    }


def grants_match(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    cur_models = sorted(str(m) for m in (current.get("models") or []))
    cur_mcp = sorted(str(s) for s in ((current.get("object_permission") or {}).get("mcp_servers") or []))
    return (
        current.get("key_alias") == desired["key_alias"]
        and cur_models == sorted(desired["models"])
        and cur_mcp == sorted(desired["object_permission"]["mcp_servers"])
    )


def reconcile(spec: list[dict[str, Any]], env: Mapping[str, str], api: KeyApi) -> list[str]:
    actions: list[str] = []
    for entry in spec:
        desired = desired_payload(entry, env)
        current = api.key_info(desired["key"])
        if current is None:
            api.generate(desired)
            actions.append(f"generated {desired['key_alias']}")
        elif grants_match(current, desired):
            actions.append(f"unchanged {desired['key_alias']}")
        else:
            api.delete(desired["key"])
            api.generate(desired)
            actions.append(f"regenerated {desired['key_alias']}")
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
        actions = reconcile(spec, os.environ, HttpKeyApi(base_url, master))
    except Exception as e:  # noqa: BLE001 - any failure is a non-zero exit for the depends_on gate
        print(f"bootstrap_keys: FAILED: {e}", file=sys.stderr)
        return 1
    for action in actions:
        print(f"bootstrap_keys: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
