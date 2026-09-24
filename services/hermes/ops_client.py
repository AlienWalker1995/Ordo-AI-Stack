"""HTTP client for the control plane's privileged verbs.

Hermes uses this in place of raw `docker` / `docker compose` shelling.
The class is intentionally narrow — every method maps to one named
control-plane endpoint. There is no `exec` or arbitrary-shell verb.

NB: these verbs used to live on a separate **ops-api** service while they were being
ported; ops-controller serves all of them now, so there is one URL
— the scheduler serves only /status, /model-config, /jobs* and /health. This
client originally pointed at the scheduler and every tool 404'd (audit P0-2,
fixed 2026-07-24).
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
        # One control plane. These verbs lived on a separate Bearer-gated `ops-api` while they
        # were being ported; ops-controller serves all of them now, so both URLs are the same
        # service and `ctl_url` below is kept only so existing callers keep working.
        self.url = url or os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
        token = token or os.environ.get("OPS_CONTROLLER_TOKEN", "")
        if not token:
            raise OpsClientError("OPS_CONTROLLER_TOKEN env var is empty")
        # X-Actor names the caller in ops-controller's audit log.
        self._headers = {"Authorization": f"Bearer {token}", "X-Actor": "hermes"}
        self._client = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout)
        # Same service as `self.url`. ControlPlane is authless by design (it trusts the
        # localhost/tailnet boundary; auth is Caddy's job), so this client sends no token.
        self.ctl_url = ctl_url or os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
        self._ctl = httpx.Client(base_url=self.ctl_url, timeout=timeout)

    def _check(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                j = r.json()
                detail = j.get("detail") or j.get("error") or r.text
            except Exception:
                detail = r.text
            raise OpsClientError(f"{r.status_code} {detail}")

    def list_containers(self) -> list[dict[str, Any]]:
        r = self._client.get("/containers")
        self._check(r)
        return r.json()

    def container_logs(self, name: str, *, tail: int = 100, since: str | None = None) -> str:
        params: dict[str, Any] = {"tail": tail}
        if since:
            params["since"] = since
        r = self._client.get(f"/containers/{name}/logs", params=params)
        self._check(r)
        return r.text

    def restart_container(self, name: str) -> dict[str, Any]:
        r = self._client.post(f"/containers/{name}/restart")
        self._check(r)
        return r.json()

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
        if verb == "down":
            r = self._client.post(f"/services/{service}/stop")
        else:  # up / restart -> recreate (picks up new .env / volumes / network)
            r = self._client.post(f"/services/{service}/recreate", json={"confirm": confirm})
        self._check(r)
        return r.json()

    # --- control-plane (ops-controller scheduler, authless): service-plugin install/enable ---
    # These are the render authority: they edit ordo.yaml's plugin list + re-render out/. They do NOT
    # start containers, the caller then brings each service up via compose_up() (ops-controller recreate).
    def list_plugins(self) -> dict[str, Any]:
        r = self._ctl.get("/plugins")
        self._check(r)
        return r.json()

    def enable_plugin(self, plugin_id: str, *, confirm: bool = False) -> dict[str, Any]:
        r = self._ctl.post(f"/plugins/{plugin_id}/enable", json={"confirm": confirm})
        self._check(r)
        return r.json()

    def disable_plugin(self, plugin_id: str, *, confirm: bool = False) -> dict[str, Any]:
        r = self._ctl.post(f"/plugins/{plugin_id}/disable", json={"confirm": confirm})
        self._check(r)
        return r.json()

    def close(self) -> None:
        self._client.close()
        self._ctl.close()
