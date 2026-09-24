"""HTTP client for the control plane's privileged verbs.

Hermes uses this in place of raw `docker` / `docker compose` shelling.
The class is intentionally narrow — every method maps to one named
control-plane endpoint. There is no `exec` or arbitrary-shell verb.

NB: these verbs used to live on a separate **ops-api** service while they were being
ported; ops-controller serves all of them now, so there is one URL
— the scheduler serves only /status, /model-config, /jobs* and /health. This
client originally pointed at the scheduler and every tool 404'd (audit P0-2,
fixed 2026-07-24).

Every public method sends its request through `_request()`, the one place that attaches
the bearer token and X-Actor header. ops-controller requires that bearer on every path but
/health (`ordo/control.py`'s `UNAUTHENTICATED_PATHS`); there is no "authless" route on it,
so no method may build its own client or call httpx directly (audit found the plugin
enable/disable/list methods doing exactly that, on a second unauthenticated client, and
401ing on every call).
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class OpsClientError(RuntimeError):
    """Raised when the control plane returns a non-2xx response."""


class OpsClient:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
        ctl_url: str | None = None,
    ):
        # One control plane serves every verb below. `ctl_url` is kept only so existing
        # callers that still pass it keep working; it always resolves to the same URL as
        # `self.url`, and every request (plugin verbs included) goes through self._client.
        self.url = url or os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
        self.ctl_url = ctl_url or self.url
        token = token or os.environ.get("OPS_CONTROLLER_TOKEN", "")
        if not token:
            raise OpsClientError("OPS_CONTROLLER_TOKEN env var is empty")
        # X-Actor names the caller in ops-controller's audit log.
        self._headers = {"Authorization": f"Bearer {token}", "X-Actor": "hermes"}
        self._client = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """The one request path every public method uses. `self._client` already carries the
        bearer and X-Actor headers on every request it sends, so a method cannot forget them
        by going around this — there is nothing to go around to."""
        r = self._client.request(method, path, json=json, params=params)
        self._check(r)
        return r

    def _check(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                j = r.json()
                detail = j.get("detail") or j.get("error") or r.text
            except Exception:
                detail = r.text
            raise OpsClientError(f"{r.status_code} {detail}")

    def list_containers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/containers").json()

    def container_logs(self, name: str, *, tail: int = 100, since: str | None = None) -> str:
        params: dict[str, Any] = {"tail": tail}
        if since:
            params["since"] = since
        return self._request("GET", f"/containers/{name}/logs", params=params).text

    def restart_container(self, name: str) -> dict[str, Any]:
        return self._request("POST", f"/containers/{name}/restart").json()

    def compose_up(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("up", service, confirm)

    def compose_down(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("down", service, confirm)

    def compose_restart(self, *, service: str | None = None, confirm: bool = False) -> dict[str, Any]:
        return self._compose("restart", service, confirm)

    def _compose(self, verb: str, service: str | None, confirm: bool) -> dict[str, Any]:
        # This client refuses stack-wide verbs itself. ops-controller DOES serve
        # POST /compose/{up,down,restart} without a service: it runs the verb on the
        # whole project, so a stack-wide down stops everything, ops-controller and the
        # GPU scheduler included. Stack lifecycle is the operator's `ordo up`. The
        # per-service equivalent is POST /services/{id}/recreate (up/restart).
        if service is None:
            raise OpsClientError(
                "stack-wide compose verbs are refused by this client (ops-controller would run "
                "them on the whole project); pass a service name for a per-service recreate"
            )
        # up / down / restart all take {"confirm": ...} the same way: ops-controller's
        # service_stop/service_start/service_restart each refuse a missing confirm with 400.
        if verb == "down":
            r = self._request("POST", f"/services/{service}/stop", json={"confirm": confirm})
        else:  # up / restart -> recreate (picks up new .env / volumes / network)
            r = self._request("POST", f"/services/{service}/recreate", json={"confirm": confirm})
        return r.json()

    # --- service-plugin install/enable: the render authority. These edit ordo.yaml's plugin
    # list + re-render out/. They do NOT start containers; the caller then brings each service
    # up via compose_up() (ops-controller recreate).
    def list_plugins(self) -> dict[str, Any]:
        return self._request("GET", "/plugins").json()

    def enable_plugin(self, plugin_id: str, *, confirm: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/plugins/{plugin_id}/enable", json={"confirm": confirm}).json()

    def disable_plugin(self, plugin_id: str, *, confirm: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/plugins/{plugin_id}/disable", json={"confirm": confirm}).json()

    def close(self) -> None:
        self._client.close()
