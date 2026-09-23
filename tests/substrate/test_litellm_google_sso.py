"""LiteLLM admin UI Google SSO: the second login step becomes the same Google identity as the
edge, reusing the stack's existing Google OAuth client - no new secret.

Mirrors tests/substrate/test_langfuse.py's URL-derivation and credential-shape coverage: the
mechanism (ordo/render.py::litellm_google_sso_env) is the same edge-derived-URL pattern as
LANGFUSE_PUBLIC_URL, gated on the same two edge shapes, plus the `${OAUTH2_PROXY_CLIENT_ID}` /
`${OAUTH2_PROXY_CLIENT_SECRET}` compose-level references that keep this a zero-new-secret change.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import litellm_google_sso_env, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}

EDGE_SITE = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net",
             "CADDY_BIND": "0.0.0.0"}


def _src(**kw):
    base = {"hardware": P_5090, "tier": "auto", "model": "auto", "plugins": ["edge"]}
    base.update(kw)
    return Source.from_dict(base)


def _gateway_env(plugins, site=None):
    rc = render(Source.from_dict({"hardware": P_5090, "plugins": plugins, "site": site or {}}),
                CATALOG, REGISTRY)
    return rc.compose_dict()["services"]["model-gateway"]["environment"]


SSO_KEYS = ("PROXY_BASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")


# ── litellm_google_sso_env() unit coverage ──────────────────────────────────────

def test_uses_the_sidecar_name_when_tailnet_names_is_enabled():
    env = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
           "CADDY_TAILNET_DOMAIN": "example.ts.net"}
    out = litellm_google_sso_env(env, ["edge", "tailnet-names"], "")
    assert out["PROXY_BASE_URL"] == "https://llm.example.ts.net"


def test_falls_back_to_the_sso_port_without_the_sidecar_layer():
    env = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net"}
    out = litellm_google_sso_env(env, ["edge"], "")
    assert out["PROXY_BASE_URL"] == "https://host.example.ts.net:8449"


def test_empty_without_an_edge_hostname():
    assert litellm_google_sso_env({}, [], "") == {}


def test_empty_when_hostname_known_but_edge_not_enabled():
    """A hostname alone (e.g. leftover site config) must not turn SSO on without the edge plugin."""
    env = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net"}
    assert litellm_google_sso_env(env, [], "") == {}


def test_google_client_vars_are_compose_references_not_values():
    env = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net"}
    out = litellm_google_sso_env(env, ["edge"], "")
    assert out["GOOGLE_CLIENT_ID"] == "${OAUTH2_PROXY_CLIENT_ID}"
    assert out["GOOGLE_CLIENT_SECRET"] == "${OAUTH2_PROXY_CLIENT_SECRET}"


def test_admin_id_present_only_when_identity_given():
    env = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net"}
    assert "PROXY_ADMIN_ID" not in litellm_google_sso_env(env, ["edge"], "")
    with_id = litellm_google_sso_env(env, ["edge"], "112233445566778899")
    assert with_id["PROXY_ADMIN_ID"] == "112233445566778899"


# ── full render -> compose wiring ───────────────────────────────────────────────

def test_edge_off_renders_none_of_the_sso_vars():
    env = _gateway_env(["monitoring"])
    for key in (*SSO_KEYS, "PROXY_ADMIN_ID"):
        assert key not in env, f"{key} rendered on model-gateway with the edge plugin disabled"


def test_edge_on_without_a_hostname_renders_none_of_the_sso_vars():
    """Edge enabled but no site hostname yet (fresh install) must not half-render the vars."""
    env = _gateway_env(["edge"])
    for key in (*SSO_KEYS, "PROXY_ADMIN_ID"):
        assert key not in env


def test_edge_on_with_hostname_renders_the_port_fallback():
    env = _gateway_env(["edge"], site=EDGE_SITE)
    assert env["PROXY_BASE_URL"] == "https://host.example.ts.net:8449"
    assert env["GOOGLE_CLIENT_ID"] == "${OAUTH2_PROXY_CLIENT_ID}"
    assert env["GOOGLE_CLIENT_SECRET"] == "${OAUTH2_PROXY_CLIENT_SECRET}"
    assert "PROXY_ADMIN_ID" not in env


def test_tailnet_names_renders_the_clean_subdomain():
    env = _gateway_env(["edge", "tailnet-names"], site=EDGE_SITE)
    assert env["PROXY_BASE_URL"] == "https://llm.example.ts.net"


def test_admin_identity_site_key_flows_to_proxy_admin_id():
    site = dict(EDGE_SITE, LITELLM_ADMIN_IDENTITY="112233445566778899")
    env = _gateway_env(["edge", "tailnet-names"], site=site)
    assert env["PROXY_ADMIN_ID"] == "112233445566778899"


def test_admin_identity_omitted_when_unset():
    env = _gateway_env(["edge", "tailnet-names"], site=EDGE_SITE)
    assert "PROXY_ADMIN_ID" not in env


def test_master_key_login_env_is_unaffected_either_way():
    """FORWARDED_ALLOW_IPS (the existing non-SSO login's dependency) must survive regardless."""
    for plugins in (["monitoring"], ["edge"], ["edge", "tailnet-names"]):
        env = _gateway_env(plugins, site=EDGE_SITE)
        assert env["FORWARDED_ALLOW_IPS"] == "*"


# ── zero-new-secret guarantee ────────────────────────────────────────────────────

def test_no_new_secret_is_declared():
    """The design reuses OAUTH2_PROXY_CLIENT_ID/SECRET (already required by the edge plugin);
    this feature must not add GOOGLE_CLIENT_ID/SECRET (or anything else) to required_secrets."""
    rc = render(_src(site=EDGE_SITE), CATALOG, REGISTRY)
    assert "GOOGLE_CLIENT_ID" not in rc.required_secrets
    assert "GOOGLE_CLIENT_SECRET" not in rc.required_secrets
    assert "OAUTH2_PROXY_CLIENT_ID" in rc.required_secrets
    assert "OAUTH2_PROXY_CLIENT_SECRET" in rc.required_secrets


def test_no_secret_value_is_inlined_in_the_rendered_compose():
    """Same structural rule as test_langfuse.py: an env value whose KEY looks credential-shaped
    must be fed by a ${...} interpolation, never a literal - GOOGLE_CLIENT_ID/_SECRET included."""
    rc = render(_src(site=EDGE_SITE, plugins=["edge", "tailnet-names"]), CATALOG, REGISTRY)
    services = rc.compose_dict()["services"]
    text = yaml.safe_dump(rc.compose_dict(), sort_keys=False)
    interpolation = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:?][^}]*)?\}")
    credential_key = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN|_AUTH|SALT|CLIENT_ID)$")
    for name, svc in services.items():
        for key, raw in (svc.get("environment") or {}).items():
            if credential_key.search(key):
                assert "${" in str(raw), f"{name}.{key} is credential-shaped but holds a literal"
    # and no env VALUE carries a secret name outside a ${...} reference (a service may use the
    # secret's own name as its variable, e.g. oauth2-proxy's OAUTH2_PROXY_CLIENT_ID)
    for name, svc in services.items():
        for key, raw in (svc.get("environment") or {}).items():
            outside = interpolation.sub("", str(raw))
            for secret in ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET"):
                assert secret not in outside, f"{name}.{key} carries {secret} outside a ${{...}} reference"
    assert text  # the whole file rendered
