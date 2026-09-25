"""Load + validate the declarative source (ordo.yaml) — the single source of truth."""
from __future__ import annotations

import dataclasses
import difflib
import re
from pathlib import Path
from typing import Any

import yaml

from .hardware import GPU, HardwareProfile

# `site:` flows verbatim into the rendered .env, so each key must be a usable env var name.
_SITE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

# The site key naming the SOPS-encrypted dotenv file that holds the operator's secrets (see
# ordo/host/secret_store.py). Unset: out/secrets.env itself is the store.
SECRETS_SOURCE_KEY = "SECRETS_SOURCE"
# The documented place for the SOPS file SECRETS_SOURCE names: a private repo checked out beside this
# one. A relative value resolves against the checkout, so the same ordo.yaml works on any host that
# keeps the two repos side by side.
DEFAULT_SECRETS_SOURCE = "../ordo-secrets/secrets.env.sops"

# Where the secret VALUES come from (ordo/host/secret_store.py). `sops` (the default when SECRETS_SOURCE is
# set) reads that file; `infisical` reads an Infisical project environment, and SECRETS_SOURCE, when
# set, is then its offline backup and where the machine identity's bootstrap credentials live.
SECRETS_BACKEND_KEY = "SECRETS_BACKEND"
SECRETS_BACKENDS = ("sops", "infisical")
INFISICAL_URL_KEY = "INFISICAL_URL"
INFISICAL_PROJECT_KEY = "INFISICAL_PROJECT"
INFISICAL_ENVIRONMENT_KEY = "INFISICAL_ENVIRONMENT"
DEFAULT_INFISICAL_ENVIRONMENT = "prod"
# The machine identities' Universal Auth credentials: read from the SOPS file or the environment,
# never from ordo.yaml (whose site keys are rendered into out/.env).
INFISICAL_CREDENTIAL_KEYS = ("INFISICAL_ORDO_CLIENT_ID", "INFISICAL_ORDO_CLIENT_SECRET",
                             "INFISICAL_ORDO_WRITER_CLIENT_ID", "INFISICAL_ORDO_WRITER_CLIENT_SECRET")
_INFISICAL_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HTTP_URL = re.compile(r"^https?://[^\s/?#]+(:\d+)?(/[^\s?#]*)?$")


@dataclasses.dataclass(frozen=True)
class SecretBackend:
    """Which store holds the secret values: `local` (out/secrets.env), `sops` or `infisical`."""
    kind: str
    sops_path: str = ""          # SECRETS_SOURCE as written (sops: the store; infisical: the backup)
    url: str = ""
    project: str = ""
    environment: str = ""


def secret_backend(site: dict[str, Any]) -> SecretBackend:
    """The secret backend a `site:` mapping selects. Raises ValueError naming the key at fault."""
    site = site or {}
    sops_path = ""
    if SECRETS_SOURCE_KEY in site:
        value = site[SECRETS_SOURCE_KEY]
        if not isinstance(value, str) or not value.strip().endswith(".sops"):
            raise ValueError(f"site: {SECRETS_SOURCE_KEY} must be the path of a SOPS-encrypted dotenv file "
                             f"ending in '.sops' (e.g. ../ordo-secrets/secrets.env.sops), got {value!r}")
        sops_path = value.strip()
    for key in INFISICAL_CREDENTIAL_KEYS:
        if key in site:
            raise ValueError(f"site: {key} is a credential: it belongs in the SOPS file (or the environment), "
                             "never in ordo.yaml, whose site keys are rendered into out/.env")
    infisical_keys = [k for k in (INFISICAL_URL_KEY, INFISICAL_PROJECT_KEY, INFISICAL_ENVIRONMENT_KEY) if k in site]
    kind = str(site.get(SECRETS_BACKEND_KEY, "") or "").strip()
    if SECRETS_BACKEND_KEY in site and kind not in SECRETS_BACKENDS:
        raise ValueError(f"site: {SECRETS_BACKEND_KEY} must be one of {list(SECRETS_BACKENDS)}, got {kind!r}")
    if kind != "infisical" and infisical_keys:
        raise ValueError(f"site: {', '.join(infisical_keys)} set without {SECRETS_BACKEND_KEY}: infisical "
                         "(dead config: set the backend, or remove them)")
    if kind == "sops" and not sops_path:
        raise ValueError(f"site: {SECRETS_BACKEND_KEY}: sops needs {SECRETS_SOURCE_KEY} (the SOPS file)")
    if kind != "infisical":
        return SecretBackend("sops" if sops_path else "local", sops_path=sops_path)
    url = str(site.get(INFISICAL_URL_KEY, "") or "").strip()
    if not _HTTP_URL.match(url):
        raise ValueError(f"site: {SECRETS_BACKEND_KEY}: infisical needs {INFISICAL_URL_KEY}, the server's "
                         f"http(s) base URL (e.g. https://infisical.example.lan), got {url!r}")
    project = str(site.get(INFISICAL_PROJECT_KEY, "") or "").strip()
    if not _INFISICAL_SLUG.match(project):
        raise ValueError(f"site: {SECRETS_BACKEND_KEY}: infisical needs {INFISICAL_PROJECT_KEY}, the project "
                         f"slug (lowercase letters, digits, '-', '_'), got {project!r}")
    environment = str(site.get(INFISICAL_ENVIRONMENT_KEY, DEFAULT_INFISICAL_ENVIRONMENT) or "").strip()
    if not _INFISICAL_SLUG.match(environment):
        raise ValueError(f"site: {INFISICAL_ENVIRONMENT_KEY} must be an environment slug (e.g. prod), "
                         f"got {environment!r}")
    return SecretBackend("infisical", sops_path=sops_path, url=url, project=project, environment=environment)


# Site keys that used to mean something: a leftover one is dead config, so it fails the load.
_RETIRED_SITE_KEYS = {
    "OPERATOR_SECRETS_DIR": "was retired: the agent's file secrets (discord_token, github_backup_pat) are "
                            "materialized from the secret store into out/secrets/. Run `ordo secrets import` "
                            "(it copies the files from this directory into the store and removes this key)",
}

# Options that used to exist: a leftover key gets a migration hint instead of a typo suggestion.
_RETIRED_KEYS = {
    "cloud_fallback": "was removed (it routed oversized GPU jobs to a route nothing polled); "
                      "delete the block",
}


def _reject_unknown_keys(where: str, keys: Any, valid: list[str]) -> None:
    """Raise ValueError naming the first key not in `valid`, with the closest valid key as a hint."""
    for key in keys:
        if key in valid:
            continue
        if where == "ordo.yaml" and key in _RETIRED_KEYS:
            raise ValueError(f"{where}: key {key!r} {_RETIRED_KEYS[key]}")
        close = difflib.get_close_matches(str(key), valid, n=1)
        hint = f"did you mean {close[0]!r}?" if close else f"valid keys: {valid}"
        raise ValueError(f"{where}: unknown key {key!r} ({hint})")


@dataclasses.dataclass
class Source:
    hardware: Any = "auto"          # "auto" or an explicit hardware spec dict
    tier: str = "auto"
    model: str = "auto"
    agent: str = "hermes"
    # Control-plane UI (pluggable, like the agent): any `services/<id>/dashboard.yaml`.
    dashboard: str = "dashboard"
    plugins: Any = "auto"           # "auto" or list[str]
    overrides: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Host/site config — NOT derived from the model and NOT secret: bind-mount roots
    # (DATA_PATH, BASE_PATH, CODE_ROOT), the edge hostnames (CADDY_*), and any other
    # operator-environment key the plugin manifests interpolate (e.g. COMFYUI_IMAGE,
    # N8N_WEBHOOK_URL). These flow verbatim into the rendered `.env` so `${DATA_PATH}` /
    # `${BASE_PATH}` etc. resolve deterministically instead of defaulting to `./data`
    # relative to out/. Kept in the source (single source of truth) so there is no
    # hand-edit of a derived output and no drift.
    site: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Electricity-derived per-token cost for the local models (usd_per_kwh, inference_watts,
    # prompt_tokens_per_second, output_tokens_per_second). Optional: empty means $0/token
    # (the historical default). See render.local_token_costs for the formula.
    cost: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Source:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        _reject_unknown_keys("ordo.yaml", data, [f.name for f in dataclasses.fields(cls)])
        s = cls(
            hardware=data.get("hardware", "auto"),
            tier=str(data.get("tier", "auto")),
            model=str(data.get("model", "auto")),
            agent=str(data.get("agent", "hermes")),
            dashboard=str(data.get("dashboard", "dashboard")),
            plugins=data.get("plugins", "auto"),
            overrides=data.get("overrides") or {},
            site=data.get("site") or {},
            cost=data.get("cost") or {},
        )
        s.validate()
        return s

    def validate(self) -> None:
        valid_tiers = {"auto", "cpu", "low", "medium", "high", "ultra"}
        if self.tier not in valid_tiers:
            raise ValueError(f"tier must be one of {sorted(valid_tiers)}, got {self.tier!r}")
        if not isinstance(self.overrides, dict):
            raise ValueError("overrides must be a mapping")
        if not isinstance(self.site, dict):
            raise ValueError("site must be a mapping of env KEY -> value")
        for key in self.site:
            if not isinstance(key, str) or not _SITE_KEY.match(key):
                raise ValueError(f"site: key {key!r} is not an env var name (expected {_SITE_KEY.pattern})")
            if key in _RETIRED_SITE_KEYS:
                raise ValueError(f"site: {key} {_RETIRED_SITE_KEYS[key]}")
        secret_backend(self.site)
        if isinstance(self.hardware, dict):
            _reject_unknown_keys("hardware", self.hardware, [f.name for f in dataclasses.fields(HardwareProfile)])
            for i, gpu in enumerate(self.hardware.get("gpus") or []):
                if isinstance(gpu, dict):
                    _reject_unknown_keys(f"hardware.gpus[{i}]", gpu, [f.name for f in dataclasses.fields(GPU)])
        if not isinstance(self.cost, dict):
            raise ValueError("cost must be a mapping")
        if self.plugins != "auto" and not isinstance(self.plugins, list):
            raise ValueError("plugins must be 'auto' or a list")
