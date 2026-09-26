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
    edge shapes - a mismatch breaks sign-in only at runtime, on the box, after a deploy.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from ordo.host import secret_store, wizard
from ordo.render import compose
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry, PluginService

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

EDGE_SITE = {"CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
             "CADDY_TAILNET_DOMAIN": "example.ts.net", "SSO_ALLOWED_EMAILS": "me@example.com",
             "CADDY_BIND": "0.0.0.0"}


def _src(**kw):
    base = {"hardware": P_5090, "tier": "auto", "model": "auto", "plugins": ["langfuse"]}
    base.update(kw)
    return Source.from_dict(base)


def _compose(rc):
    return rc.compose_dict()["services"]


# ── opt-in gate ────────────────────────────────────────────────────────────────

def test_langfuse_is_not_enabled_by_auto():
    """`default: false` - six containers must never appear because the box happens to fit them."""
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


def test_every_declared_secret_reaches_the_containers_only_as_a_reference():
    """Secrets reach the containers as `${VAR}` refs resolved from secrets.env at compose time
    (the litellm-db pattern): an empty value fails the service loudly instead of defaulting.

    Three halves, because each alone passes trivially:
      1. every secret the plugin DECLARES is actually consumed by some service (a declared but
         unused key is a lie in secrets.env.example);
      2. inside an env value, a secret name only ever appears inside a `${...}` interpolation -
         never as a literal;
      3. rendering while the generated VALUES are known puts none of them in the compose text.
    """
    plugin = REGISTRY.get("langfuse")
    rendered = render(_src(), CATALOG, REGISTRY)
    services = rendered.compose_dict()["services"]
    text = yaml.safe_dump(rendered.compose_dict(), sort_keys=False)

    # Compose interpolation: ${NAME}, ${NAME:-default}, ${NAME:?message}.
    interpolation = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:?][^}]*)?\}")

    referenced: set[str] = set()
    for name in LANGFUSE_SERVICES:
        for key, raw in (services[name].get("environment") or {}).items():
            value = str(raw)
            referenced |= {m.group(1) for m in interpolation.finditer(value)}
            # strip every valid interpolation; any secret NAME left over is a literal
            literal = interpolation.sub("", value)
            for secret in plugin.secrets:
                assert secret not in literal, (
                    f"{name}.{key} carries {secret} outside a ${{...}} reference: {value!r}")

    # (1) nothing declared is dead weight
    unused = [s for s in plugin.secrets if s not in referenced]
    assert not unused, f"declared in `secrets:` but consumed by no service: {unused}"

    # (3) with real values in hand, none may be baked into the rendered compose
    values = {key: secret_store.SECRET_GENERATORS[key]() for key in plugin.secrets}
    for key, value in values.items():
        assert value not in text, f"the VALUE of {key} was inlined into the rendered compose"

    # (4) the structural rule that catches an ARBITRARY literal (`POSTGRES_PASSWORD: hunter2`
    # passes 1-3: it names no secret and matches no freshly generated value): every env key
    # that carries a credential by name must be fed by a ${...} interpolation, never a literal.
    # Anchored on the credential NOUN at the end of the key: AUTH_DISABLE_SIGNUP is a flag and
    # LANGFUSE_S3_*_ACCESS_KEY_ID is a username, neither holds a secret.
    credential_key = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN|_AUTH|SALT)$")
    for name in LANGFUSE_SERVICES:
        for key, raw in (services[name].get("environment") or {}).items():
            if credential_key.search(key):
                assert "${" in str(raw), (
                    f"{name}.{key} is credential-shaped but holds a literal: {str(raw)[:8]!r}...")


def test_langfuse_db_reuses_the_substrate_postgres_pin():
    """One Postgres version across the stack. The manifest schema has no image-alias mechanism,
    so langfuse-db copies ordo.render.compose.POSTGRES_IMAGE - and this test is what stops the copy
    drifting: bumping litellm-db's pin without bumping this one fails here, not in production."""
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    assert svcs["langfuse-db"]["image"] == compose.POSTGRES_IMAGE


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
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://langfuse.example.ts.net"


def test_public_url_falls_back_to_the_sso_port_without_the_sidecar_layer():
    rc = render(_src(plugins=["edge", "langfuse"], site=EDGE_SITE), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_PUBLIC_URL"] == "https://host.example.ts.net:8450"


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
        assert secret_store.generator_for(key) is not None, f"{key} has no wizard generator"


def test_encryption_key_is_exactly_64_hex_chars():
    """Langfuse REFUSES TO BOOT on anything else - the one generator shape that is a hard
    server-side requirement rather than a strength preference."""
    value = secret_store.SECRET_GENERATORS["LANGFUSE_ENCRYPTION_KEY"]()
    assert len(value) == 64
    assert all(c in "0123456789abcdef" for c in value)


def test_project_keys_carry_the_prefixes_langfuse_issues():
    """The Hermes plugin rejects any other shape as a leftover placeholder (it would otherwise
    build a client that silently drops every trace at flush)."""
    assert secret_store.SECRET_GENERATORS["LANGFUSE_PUBLIC_KEY"]().startswith("pk-lf-")
    assert secret_store.SECRET_GENERATORS["LANGFUSE_SECRET_KEY"]().startswith("sk-lf-")


def test_salt_and_encryption_key_are_never_rotated():
    """Rotating either makes stored API keys unmatchable and stored secrets undecryptable, so
    `ordo secrets rotate` refuses them (the LITELLM_SALT_KEY rule)."""
    for key in ("LANGFUSE_SALT", "LANGFUSE_ENCRYPTION_KEY"):
        assert "never rotate" in secret_store.rotation_refusal(key)
        assert not secret_store.is_internal(key)
    for key in ("LANGFUSE_DB_PASSWORD", "LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_REDIS_AUTH",
                "LANGFUSE_MINIO_SECRET", "LANGFUSE_NEXTAUTH_SECRET"):
        assert secret_store.rotation_refusal(key) is None and secret_store.is_internal(key), \
            f"{key} should be rotatable"


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
    resolve - and only an interactive sign-in would ever notice."""
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
    # `hermes`, not the gateway's `gateway`: the two writers trace the same LLM calls and are
    # told apart only by environment.
    assert env["HERMES_LANGFUSE_ENV"] == "hermes"
    assert "langfuse" not in agent_yaml.get("depends_on", {})


def test_hermes_entrypoint_enables_the_plugin_only_with_a_key():
    """Enabling the bundled plugin without credentials leaves hooks permanently inert while the
    CLI reports "enabled" - tracing that looks configured and records nothing."""
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
    assert 'uv pip install "langfuse==4.15.1"' in dockerfile


def test_app_healthchecks_probe_the_bound_address_not_localhost():
    """Both Langfuse app services bind ONLY their container's eth0 address, so a localhost probe
    can never connect and the service sits permanently unhealthy while serving every peer fine
    (observed live 2026-09-14: `172.25.0.39:3000 LISTEN`, localhost refused)."""
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in ("langfuse-web", "langfuse-worker"):
        probe = " ".join(svcs[name]["healthcheck"]["test"])
        assert "$(hostname)" in probe, f"{name} healthcheck must probe the address it binds"
        assert "localhost" not in probe, f"{name} healthcheck probes localhost, which is not bound"


# ── retention (langfuse-retention + langfuse-minio-lifecycle) ──────────────────

RETENTION_SERVICES = ("langfuse-retention", "langfuse-minio-lifecycle")


def test_retention_days_default_is_scoped_to_its_two_consumers():
    """90 days, as ONE compose default on both consumers, and NOT a derived .env key: the default
    stays scoped to the two services that read it."""
    rc = render(_src(), CATALOG, REGISTRY)
    assert "LANGFUSE_RETENTION_DAYS" not in rc.env
    svcs = _compose(rc)
    for name in RETENTION_SERVICES:
        assert svcs[name]["environment"]["LANGFUSE_RETENTION_DAYS"] == "${LANGFUSE_RETENTION_DAYS:-90}"


def test_site_can_override_the_retention_days():
    rc = render(_src(site={"LANGFUSE_RETENTION_DAYS": "30"}), CATALOG, REGISTRY)
    assert rc.env["LANGFUSE_RETENTION_DAYS"] == "30"


def test_retention_services_render_pinned_behind_the_profile_with_no_ports():
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    for name in RETENTION_SERVICES:
        assert svcs[name]["profiles"] == ["langfuse"]
        assert "@sha256:" in svcs[name]["image"], f"{name} image is not digest-pinned"
        assert "ports" not in svcs[name]
        assert svcs[name]["security_opt"] == ["no-new-privileges:true"]


def test_retention_job_runs_the_tracked_module_on_a_daily_schedule():
    svc = _compose(render(_src(), CATALOG, REGISTRY))["langfuse-retention"]
    assert svc["command"] == ["python", "/app/langfuse_retention.py", "--daemon"]
    assert svc["restart"] == "unless-stopped"
    assert svc["depends_on"]["langfuse-web"] == {"condition": "service_healthy"}
    assert svc["environment"]["LANGFUSE_BASE_URL"] == "http://langfuse-web:3000"
    # the key pair is two files, read by the script with its mounted secret_env.py
    assert svc["environment"]["LANGFUSE_PUBLIC_KEY_FILE"] == "/run/secrets/langfuse_public_key"
    assert svc["environment"]["LANGFUSE_SECRET_KEY_FILE"] == "/run/secrets/langfuse_secret_key"
    assert "LANGFUSE_PUBLIC_KEY" not in svc["environment"] and "LANGFUSE_SECRET_KEY" not in svc["environment"]
    assert any(v.endswith("/services/langfuse/secret_env.py:/app/secret_env.py:ro") for v in svc["volumes"])
    assert svc["healthcheck"]["test"] == ["CMD", "python", "/app/langfuse_retention.py", "--healthcheck"]
    mount = svc["volumes"][0]
    assert mount.endswith("/services/langfuse/langfuse_retention.py:/app/langfuse_retention.py:ro")
    assert (ROOT / "services" / "langfuse" / "langfuse_retention.py").is_file()


def test_retention_job_reuses_the_stack_python_base_pin():
    """One Python base digest to audit: the one ops-controller builds FROM."""
    svc = _compose(render(_src(), CATALOG, REGISTRY))["langfuse-retention"]
    digest = svc["image"].split("@", 1)[1]
    dockerfile = (ROOT / "services" / "ops-controller" / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM python@{digest}" in dockerfile


def test_minio_lifecycle_is_an_idempotent_one_shot_on_the_minio_pin():
    svcs = _compose(render(_src(), CATALOG, REGISTRY))
    svc = svcs["langfuse-minio-lifecycle"]
    assert svc["image"] == svcs["langfuse-minio"]["image"]
    assert svc["restart"] == "on-failure"
    assert svc["depends_on"]["langfuse-minio"] == {"condition": "service_healthy"}
    script = svc["entrypoint"][-1]
    # import REPLACES the lifecycle config (idempotent); `rule add` would append a duplicate each run
    assert "mc ilm import langfuse/langfuse" in script and "rule add" not in script
    assert '"ID":"ordo-langfuse-retention"' in script and '"Days":%s' in script
    assert "mc ilm rule ls langfuse/langfuse" in script
    # the secret reaches mc from its file, never the environment or a command-line argument
    assert svc["environment"]["LANGFUSE_MINIO_SECRET_FILE"] == "/run/secrets/langfuse_minio_secret"
    assert "LANGFUSE_MINIO_SECRET" not in svc["environment"]
    assert '$$(cat "$$LANGFUSE_MINIO_SECRET_FILE")' in script


def test_plugin_service_restart_policy_is_validated():
    assert PluginService.from_dict({"name": "x", "image": "i", "restart": "on-failure"}).restart == "on-failure"
    assert PluginService.from_dict({"name": "x", "image": "i", "restart": False}).restart == "no"
    assert PluginService.from_dict({"name": "x", "image": "i"}).restart == ""
    with pytest.raises(ValueError, match="restart"):
        PluginService.from_dict({"name": "x", "image": "i", "restart": "always"})
