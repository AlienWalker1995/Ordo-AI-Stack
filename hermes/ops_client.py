"""HTTP client for ops-controller's privileged verbs.

Hermes uses this in place of raw `docker` / `docker compose` shelling.
The class is intentionally narrow — every method maps to one named
ops-controller endpoint. There is no `exec` or arbitrary-shell verb.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class OpsClientError(RuntimeError):
    """Raised when ops-controller returns a non-2xx response."""


class OpsClient:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
    ):
        self.url = url or os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
        token = token or os.environ.get("OPS_CONTROLLER_TOKEN", "")
        if not token:
            raise OpsClientError("OPS_CONTROLLER_TOKEN env var is empty")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.Client(base_url=self.url, headers=self._headers, timeout=timeout)

    def _check(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
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
        body = {"service": service, "confirm": confirm}
        r = self._client.post(f"/compose/{verb}", json=body)
        self._check(r)
        return r.json()

    def close(self) -> None:
        self._client.close()
