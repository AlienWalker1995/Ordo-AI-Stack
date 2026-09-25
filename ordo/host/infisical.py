"""The thin client for a (self-hosted) Infisical server: the one file that knows its HTTP API.

`ordo/host/secret_store.py` uses it when `site: SECRETS_BACKEND: infisical`. It speaks four calls:

- Universal Auth login: `POST /api/v1/auth/universal-auth/login` ({clientId, clientSecret} -> accessToken)
- project by slug:      `GET /api/v1/projects/slug/{slug}` (-> the project, whose `id` the secret calls take)
- read an environment:  `GET /api/v3/secrets/raw?workspaceId=..&environment=..&secretPath=/`
- write one secret:     `POST|PATCH|DELETE /api/v3/secrets/raw/{name}` (a writer identity only)

The transport is the stdlib (urllib): the CLI core is PyYAML-only, so this adds no dependency. Errors
name the server, the call and the HTTP status, never a credential, a token or a secret value: response
bodies are parsed for the fields this client needs and are otherwise never quoted.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

# Seconds per HTTP call. The server is on the operator's own network; a stall is a fault, not load.
TIMEOUT_SECONDS = 15.0

# The folder of the environment Ordo reads and writes: the project root.
SECRET_PATH = "/"

# A transport: (method, url, headers, body or None, timeout) -> (HTTP status, response body).
# It raises InfisicalError when the server cannot be reached at all.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]


class InfisicalError(Exception):
    """An Infisical call failed. The message names the server, the call and the reason, never a value."""


def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
         timeout: float) -> tuple[int, bytes]:
    """The stdlib transport. An HTTP error status is returned, not raised; only an unreachable server
    (DNS, refused connection, TLS failure, timeout) raises."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        parts = urllib.parse.urlsplit(url)
        raise InfisicalError(f"cannot reach Infisical at {parts.scheme}://{parts.netloc}: {reason}") from None


class InfisicalClient:
    """One machine identity (Universal Auth) against one server. Logs in on first use and keeps the
    access token and resolved project ids in memory for the life of the object (one CLI command)."""

    def __init__(self, base_url: str, client_id: str, client_secret: str, *, identity: str,
                 id_names: str, transport: Transport | None = None, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        # How errors name this identity ("reader identity") and where its credentials come from.
        self.identity = identity
        self.id_names = id_names
        self._transport = transport
        self._timeout = timeout
        self._token: str | None = None
        self._project_ids: dict[str, str] = {}

    # -- HTTP ---------------------------------------------------------------- #

    def _call(self, method: str, path: str, *, query: Mapping[str, str] | None = None,
              payload: Mapping[str, Any] | None = None, auth: bool = True) -> tuple[int, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._login()}"
        # Looked up at call time so a test can swap the module's transport.
        transport = self._transport or send
        status, raw = transport(method, url, headers, body, self._timeout)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return status, data

    def _fail(self, what: str, status: int) -> InfisicalError:
        if status == 401:
            return InfisicalError(f"Infisical at {self.base_url} ({what}): {self.identity} identity credentials "
                                  f"rejected (HTTP 401): check {self.id_names}")
        return InfisicalError(f"Infisical at {self.base_url} ({what}) failed: HTTP {status}")

    def _login(self) -> str:
        if self._token is None:
            status, data = self._call("POST", "/api/v1/auth/universal-auth/login", auth=False,
                                      payload={"clientId": self._client_id, "clientSecret": self._client_secret})
            token = data.get("accessToken") if status == 200 and isinstance(data, dict) else None
            if status in (401, 403):
                raise self._fail("universal-auth login", 401)
            if not token:
                raise self._fail("universal-auth login", status)
            self._token = str(token)
        return self._token

    # -- the calls ----------------------------------------------------------- #

    def project_id(self, slug: str) -> str:
        if slug not in self._project_ids:
            status, data = self._call("GET", f"/api/v1/projects/slug/{urllib.parse.quote(slug, safe='')}")
            if status == 404:
                raise InfisicalError(f"Infisical at {self.base_url}: no project with slug '{slug}' "
                                     f"(or the {self.identity} identity is not a member of it)")
            if status == 403:
                raise InfisicalError(f"Infisical at {self.base_url}: {self.identity} identity lacks read on "
                                     f"project '{slug}' (HTTP 403)")
            project_id = data.get("id") if status == 200 and isinstance(data, dict) else None
            if not project_id:
                raise self._fail(f"project '{slug}'", status)
            self._project_ids[slug] = str(project_id)
        return self._project_ids[slug]

    def read_secrets(self, slug: str, environment: str) -> dict[str, str]:
        """{KEY: value} of the environment's root folder (shared secrets; imports are not followed)."""
        query = {"workspaceId": self.project_id(slug), "environment": environment, "secretPath": SECRET_PATH}
        status, data = self._call("GET", "/api/v3/secrets/raw", query=query)
        if status == 403:
            raise InfisicalError(f"Infisical at {self.base_url}: {self.identity} identity lacks read on project "
                                 f"'{slug}' environment '{environment}' (HTTP 403)")
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("secrets"), list):
            raise self._fail(f"read project '{slug}' environment '{environment}'", status)
        values: dict[str, str] = {}
        hidden: list[str] = []
        for item in data["secrets"]:
            if not isinstance(item, dict) or item.get("type", "shared") != "shared":
                continue
            key = str(item.get("secretKey", ""))
            if item.get("secretValueHidden"):
                hidden.append(key)
                continue
            values[key] = str(item.get("secretValue", ""))
        if hidden:
            raise InfisicalError(f"Infisical project '{slug}' environment '{environment}': values are hidden from "
                                 f"the {self.identity} identity (it may list but not read them): "
                                 f"{', '.join(hidden)}. Give its role read access to secret values")
        return values

    def write_secret(self, slug: str, environment: str, method: str, key: str, value: str | None = None) -> None:
        """Create (POST), update (PATCH) or delete (DELETE) one shared secret."""
        payload: dict[str, Any] = {"workspaceId": self.project_id(slug), "environment": environment,
                                   "secretPath": SECRET_PATH, "type": "shared"}
        if value is not None:
            payload["secretValue"] = value
        status, data = self._call(method, f"/api/v3/secrets/raw/{urllib.parse.quote(key, safe='')}",
                                  payload=payload)
        if status == 403:
            raise InfisicalError(f"Infisical at {self.base_url}: {self.identity} identity lacks write on project "
                                 f"'{slug}' environment '{environment}' (HTTP 403, {method} {key})")
        if status != 200:
            raise self._fail(f"{method} {key}", status)
        if isinstance(data, dict) and "approval" in data and "secret" not in data:
            raise InfisicalError(f"Infisical project '{slug}': the change to {key} waits for approval (a secret "
                                 "approval policy covers this environment): approve it in the Infisical UI")
