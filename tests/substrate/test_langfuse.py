"""The `langfuse` plugin: render shape, opt-in gate, URL derivation, and secret generators.

Langfuse is the first plugin that is OPT-IN (`default: false`) and the first that declares the
generic compose fields added with it (entrypoint / ulimits / security_opt / resources /
depends_on conditions). Both are easy to lose in a refactor and neither fails loudly at render
time, so they are locked here:

  * enabling it must never happen implicitly (six containers appearing behind `plugins: auto`),
  * every one of its six images must stay digest-pinned (pin-don't-float),
  * none of them may publish a host port (the edge is the only front door),
  * all ten secrets must reach `required_secrets` (a dropped one renders a service that starts
    with an empty password instead of failing),
  * LANGFUSE_PUBLIC_URL must equal the URL the browser will actually use, for each of the three
    edge shapes — a mismatch breaks sign-in only at runtime, on the box, after a deploy.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ordo import wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}

LANGFUSE_SERVICES = ("langfuse-db", "langfuse-clickhouse", "langfuse-redis",
                     "langfuse-minio", "langfuse-worker", "langfuse-web")

LANGFUSE_SECRETS = ("LANGFUSE_DB_PASSWORD", "LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_REDIS_AUTH",
                    "LANGFUSE_MINIO_SECRET", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
                    "LANGFUSE_ENCRYPTION_KEY", "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_PUBLIC_KEY",
                    "LANGFUSE_SECRET_KEY")

EDGE_SITE = {"CADDY_TAILNET_HOSTNAME": "ultracam.tail63bdfc.ts.net",
             "CADDY_TAILNET_DOMAIN": "tail63bdfc.ts.net",
             "CADDY_BIND": "0.0.0.0"}


def _src(**kw):
    base = {"hardware": P_5090, "tier": "auto", "model": "auto", "plugins": ["langfuse"]}
    base.update(kw)
    return Source.from_dict(base)


def _compose(rc):
    return rc.compose_dict()["services"]


# ── opt-in gate ────────────────────────────────────────────────────────────────

def test_langfuse_is_not_enabled_by_auto():
    """`default: false` — six containers must never appear because the box happens to fit them."""
    rc = render(Source.from_dict({"hardware": P_5090, "plugins": "auto"}), CATALOG, REGISTRY)
    assert "langfuse" not in rc.plugins_enabled
    assert "LANGFUSE_ENABLED" not in rc.env


def test_langfuse_enables_when_listed_explicitly():
    rc = render(_src(), CATALOG, REGISTRY)
    assert rc.plugins_enabled == ["langfuse"]
    assert rc.env["LANGFUSE_ENABLED"] == "1"
    assert "langfuse" in rc.compose_profiles


def test_wizard_capability_path_never_adds_an_opt_in_plugin():
    """The capability screen does not offer langfuse, so the list it writes must not contain it."""
    all_ids = [p.id for p in REGISTRY.plugins if p.default]
    assert "langfuse" not in all_ids
    kept = [c for c in wizard.CAPABILITIES if c != "voice"]
    assert "langfuse" not in wizard.plugins_from_capabilities(kept, all_ids)


# ── rendered compose shape ─────────────────────────────────────────────────────

def test_all_six_services_render_behind_the_profile():
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in LANGFUSE_SERVICES:
        assert name in svcs, f"{name} missing from the rendered compose"
        assert svcs[name]["profiles"] == ["langfuse"]


def test_every_image_is_digest_pinned():
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in LANGFUSE_SERVICES:
        image = svcs[name]["image"]
        assert "@sha256:" in image, f"{name} image is not digest-pinned: {image}"
        assert not image.endswith(":latest"), f"{name} rides a rolling tag: {image}"


def test_no_langfuse_service_publishes_a_host_port():
    """Every UI is reached THROUGH the edge; a host port here would be an unauthenticated door."""
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in LANGFUSE_SERVICES:
        assert "ports" not in svcs[name], f"{name} publishes a host port"


def test_no_secret_value_is_inlined_in_the_rendered_compose():
    """Secrets reach the containers as `${VAR}` refs resolved from secrets.env at compose time
    (the litellm-db pattern): an empty value fails the service loudly instead of defaulting."""
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in LANGFUSE_SERVICES:
        for key, value in (svcs[name].get("environment") or {}).items():
            if any(s in str(value) for s in LANGFUSE_SECRETS):
                assert str(value).startswith("${") or "${" in str(value), (
                    f"{name}.{key} must reference a secret, not inline it")


def test_datastores_are_gated_service_healthy_before_the_app_starts():
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for app in ("langfuse-worker", "langfuse-web"):
        dep = svcs[app]["depends_on"]
        for store in ("langfuse-db", "langfuse-clickhouse", "langfuse-redis", "langfuse-minio"):
            assert dep[store] == {"condition": "service_healthy"}, (
                f"{app} must wait for {store} to be HEALTHY, not merely created")
    # web orders after the worker's migration runner without gating on its health
    assert svcs["langfuse-web"]["depends_on"]["langfuse-worker"] == {"condition": "service_started"}


def test_generic_compose_fields_render():
    """The manifest fields added for this plugin must actually reach the compose file."""
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    assert svcs["langfuse-minio"]["entrypoint"][0] == "sh"
    assert "mkdir -p /data/langfuse" in svcs["langfuse-minio"]["entrypoint"][-1]
    assert svcs["langfuse-clickhouse"]["ulimits"]["nofile"] == {"soft": 262144, "hard": 262144}
    assert svcs["langfuse-web"]["security_opt"] == ["no-new-privileges:true"]
    assert svcs["langfuse-web"]["deploy"]["resources"]["limits"] == {"cpus": "4", "memory": "4g"}


def test_named_volumes_are_declared():
    compose = render(_src(), CATALOG, REGISTRY).compose_dict()
    for vol in ("langfuse-db-data", "langfuse-clickhouse-data", "langfuse-clickhouse-logs",
                "langfuse-minio-data", "langfuse-redis-data"):
        assert vol in compose["volumes"], f"{vol} missing from the top-level volumes"


def test_headless_init_wires_the_project_keys_hermes_uses():
    env = _compose(render(_src(), CATALOG, REGISTRY))["langfuse-web"]["environment"]
    assert env["LANGFUSE_INIT_ORG_ID"] == "ordo"
    assert env["LANGFUSE_INIT_PROJECT_ID"] == "hermes"
    assert env["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"] == "${LANGFUSE_PUBLIC_KEY}"
    assert env["LANGFUSE_INIT_PROJECT_SECRET_KEY"] == "${LANGFUSE_SECRET_KEY}"
    assert env["AUTH_DISABLE_SIGNUP"] == "true"
    assert env["TELEMETRY_ENABLED"] == "false"


def test_all_ten_secrets_are_required():
    rc = render(_src(), CATALOG, REGISTRY)
    missing = [s for s in LANGFUSE_SECRETS if s not in rc.required_secrets]
    assert not missing, f"secrets.env.example would omit: {missing}"


# ── LANGFUSE_PUBLIC_URL derivation (the three edge shapes) ─────────────────────

def test_public_url_uses_the_sidecar_name_when_tailnet_names_is_enabled():
    rc = render(_src(plugins=["edge", "tailnet-names", "langfuse"], site=EDGE_SITE),
                CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://langfuse.tail63bdfc.ts.net"


def test_public_url_falls_back_to_the_sso_port_without_the_sidecar_layer():
    rc = render(_src(plugins=["edge", "langfuse"], site=EDGE_SITE), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://ultracam.tail63bdfc.ts.net:8450"


def test_public_url_is_empty_without_an_edge_hostname():
    """A local install still boots Langfuse; only the browser sign-in needs the origin."""
    rc = render(_src(), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == ""


def test_site_can_override_the_derived_public_url_and_admin_email():
    site = dict(EDGE_SITE, LANGFUSE_PUBLIC_URL="https://traces.example.com",
                LANGFUSE_ADMIN_EMAIL="ops@example.com")
    rc = render(_src(plugins=["edge", "tailnet-names", "langfuse"], site=site), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://traces.example.com"
    assert rc.env["LANGFUSE_ADMIN_EMAIL"] == "ops@example.com"


def test_admin_email_defaults_and_the_keys_are_absent_when_the_plugin_is_off():
    rc = render(_src(), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_ADMIN_EMAIL"] == "admin@ordo.local"
    off = render(Source.from_dict({"hardware": P_5090, "plugins": ["monitoring"]}),
                 CATALOG, REGISTRY)
    assert "LANGFUSE_PUBLIC_URL" not in off.env
    assert "LANGFUSE_ADMIN_EMAIL" not in off.env


# ── secret generators ──────────────────────────────────────────────────────────

def test_every_langfuse_secret_has_a_generator():
    """All ten are internal (no external authority issues them), so the wizard must mint all ten
    rather than prompting the operator for a value only the stack itself can know."""
    for key in LANGFUSE_SECRETS:
        assert wizard.generator_for(key) is not None, f"{key} has no wizard generator"


def test_encryption_key_is_exactly_64_hex_chars():
    """Langfuse REFUSES TO BOOT on anything else — the one generator shape that is a hard
    server-side requirement rather than a strength preference."""
    value = wizard.SECRET_GENERATORS["LANGFUSE_ENCRYPTION_KEY"]()
    assert len(value) == 64
    assert all(c in "0123456789abcdef" for c in value)


def test_project_keys_carry_the_prefixes_langfuse_issues():
    """The Hermes plugin rejects any other shape as a leftover placeholder (it would otherwise
    build a client that silently drops every trace at flush)."""
    assert wizard.SECRET_GENERATORS["LANGFUSE_PUBLIC_KEY"]().startswith("pk-lf-")
    assert wizard.SECRET_GENERATORS["LANGFUSE_SECRET_KEY"]().startswith("sk-lf-")


def test_salt_and_encryption_key_are_never_rotated():
    """Rotating either makes stored API keys unmatchable and stored secrets undecryptable, so the
    rotation script must pass them through untouched (the LITELLM_SALT_KEY rule)."""
    script = (ROOT / "scripts" / "secrets" / "rotate-internal.sh").read_text(encoding="utf-8")
    for key in ("LANGFUSE_SALT", "LANGFUSE_ENCRYPTION_KEY"):
        assert f"/^{key}=/" in script, f"{key} must be explicitly passed through, not fall through"
    for key in ("LANGFUSE_DB_PASSWORD", "LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_REDIS_AUTH",
                "LANGFUSE_MINIO_SECRET", "LANGFUSE_NEXTAUTH_SECRET"):
        assert f'print "{key}"' in script, f"{key} should be rotatable"


# ── edge + sidecar + dashboard card (the three must agree on one name/port) ────

def test_edge_publishes_the_langfuse_port():
    rc = render(_src(plugins=["edge", "langfuse"], site=EDGE_SITE), CATALOG, REGISTRY)
    caddy = rc.compose_dict()["services"]["caddy"]
    assert any(p.endswith(":8450:8450") for p in caddy["ports"]), (
        "the edge must publish :8450 or the Caddyfile's langfuse site is unreachable")


def test_sidecar_serve_asset_targets_the_same_port_the_edge_publishes():
    """Three files have to agree on one number; a mismatch is a 502 only a browser finds."""
    serve = json.loads((ROOT / "assets" / "tailscale-serve" / "langfuse.json")
                       .read_text(encoding="utf-8"))
    handler = serve["Web"]["${TS_CERT_DOMAIN}:443"]["Handlers"]["/"]["Proxy"]
    assert handler.endswith(":8450")
    caddyfile = (ROOT / "auth" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    assert "import sso_service langfuse-web:3000" in caddyfile


def test_sidecar_hostname_matches_the_derived_public_url_label():
    """`tailscale serve` registers TS_HOSTNAME as the node name; LANGFUSE_PUBLIC_URL is built
    from the same label. If they drift, Langfuse redirects post-login to a host that does not
    resolve — and only an interactive sign-in would ever notice."""
    manifest = yaml.safe_load(
        (ROOT / "services" / "tailnet-names" / "plugin.yaml").read_text(encoding="utf-8"))
    sidecar = next(s for s in manifest["services"] if s["name"] == "tailnet-langfuse")
    rc = render(_src(plugins=["edge", "tailnet-names", "langfuse"], site=EDGE_SITE),
                CATALOG, REGISTRY)
    host = rc.env["LANGFUSE_PUBLIC_URL"].removeprefix("https://").split(".", 1)[0]
    assert sidecar["env"]["TS_HOSTNAME"] == host == "langfuse"


def test_dashboard_card_probes_the_internal_health_endpoint():
    card = json.loads((ROOT / "services" / "langfuse" / "catalog.json")
                      .read_text(encoding="utf-8"))["cards"][0]
    assert card["plugin"] == "langfuse"           # gated off the render's enabled set
    assert card["tailnet_label"] == "langfuse"
    assert card["check"] == "http://langfuse-web:3000/api/public/health"
    assert "ops_service" not in card, (
        "a single lifecycle button cannot restart six coupled containers correctly")


# ── Hermes wiring (fail-open when the plugin is off) ───────────────────────────

def test_hermes_reads_the_project_keys_with_an_empty_fallback():
    """The agent must render identically whether or not langfuse is enabled: empty keys leave the
    bundled Hermes plugin inert, so there is no depends_on and no failure mode when it is off."""
    agent_yaml = yaml.safe_load(
        (ROOT / "services" / "hermes" / "agent.yaml").read_text(encoding="utf-8"))
    env = agent_yaml["environment"]
    assert env["HERMES_LANGFUSE_PUBLIC_KEY"] == "${LANGFUSE_PUBLIC_KEY:-}"
    assert env["HERMES_LANGFUSE_SECRET_KEY"] == "${LANGFUSE_SECRET_KEY:-}"
    assert env["HERMES_LANGFUSE_BASE_URL"] == "${HERMES_LANGFUSE_BASE_URL:-http://langfuse-web:3000}"
    assert env["HERMES_LANGFUSE_ENV"] == "ordo"
    assert "langfuse" not in agent_yaml.get("depends_on", {})


def test_hermes_entrypoint_enables_the_plugin_only_with_a_key():
    """Enabling the bundled plugin without credentials leaves hooks permanently inert while the
    CLI reports "enabled" — tracing that looks configured and records nothing."""
    entrypoint = (ROOT / "services" / "hermes" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'plugins enable observability/langfuse' in entrypoint
    assert '[ -n "${HERMES_LANGFUSE_PUBLIC_KEY:-}" ]' in entrypoint
    # its OWN sentinel: sharing push-through's would never fire on a stack that adds langfuse later
    assert ".ordo-langfuse-seeded" in entrypoint
    assert ".ordo-push-through-seeded" in entrypoint


def test_hermes_image_pins_the_langfuse_sdk():
    """The bundled plugin fails open when the SDK is missing, so an unpinned or absent install
    is silent. Pin it, and pin it to the major that matches the self-hosted server."""
    dockerfile = (ROOT / "services" / "hermes" / "Dockerfile").read_text(encoding="utf-8")
    assert 'uv pip install "langfuse==4.15.2"' in dockerfile
