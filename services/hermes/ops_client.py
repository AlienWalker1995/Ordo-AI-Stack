"""HTTP client for the ops-api control plane's privileged verbs.

Hermes uses this in place of raw `docker` / `docker compose` shelling.
The class is intentionally narrow — every method maps to one named
ops-api endpoint. There is no `exec` or arbitrary-shell verb.

NB: the container/compose verbs live on the **ops-api** service (Bearer-gated,
`http://ops-api:9000`), NOT on the `ordo serve` scheduler at ops-controller:9000
— the scheduler serves only /status, /model-config, /jobs* and /health. This
client originally pointed at the scheduler and every tool 404'd (audit P0-2,
fixed 2026-07-24).
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class OpsClientError(RuntimeError):
    """Raised when ops-api returns a non-2xx response."""


class OpsClient:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
        ctl_url: str | None = None,
    ):
        # OPS_API_URL, deliberately NOT OPS_CONTROLLER_URL: that var points at the
        # scheduler, which has none of these routes (the original mis-wiring).
        self.url = url or os.environ.get("OPS_API_URL", "http://ops-api:9000")
        token = token or os.environ.get("OPS_CONTROLLER_TOKEN", "")
        if not token:
            raise OpsClientError("OPS_CONTROLLER_TOKEN env var is empty")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout)
        # The ordo-serve scheduler (ops-controller) — service-plugin install/render lives HERE (the
        # render authority: /plugins), separate from the Bearer-gated ops-api verbs above. ControlPlane
        # is authless by design (it trusts the localhost/tailnet boundary; auth is Caddy's job), so
        # this client sends no token.
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
        # ops-api's stack-wide /compose/* endpoints are a deliberate 501 (compose
        # mutations are the render pipeline's job). The supported per-service
        # equivalent is POST /services/{id}/recreate (up/restart) — use it.
        if service is None:
            raise OpsClientError(
                "stack-wide compose verbs are disabled on ops-api (501 by design); "
                "pass a service name for a per-service recreate, or use the render pipeline"
            )
        if verb == "down":
            r = self._client.post(f"/services/{service}/stop")
        else:  # up / restart -> recreate (picks up new .env / volumes / network)
            r = self._client.post(f"/services/{service}/recreate", json={"confirm": confirm})
        self._check(r)
        return r.json()

    # --- control-plane (ops-controller scheduler, authless): service-plugin install/enable ---
    # These are the render authority: they edit ordo.yaml's plugin list + re-render out/. They do NOT
    # start containers — the caller then brings each service up via compose_up() (ops-api recreate).
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
