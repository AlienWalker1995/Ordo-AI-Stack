"""`ordo remote enable` / `ordo remote disable`: remote access as a later, reversible opt-in.

A fresh install is local-only: the UIs publish loopback ports and nothing asks for a Tailscale or
Google account. Enabling remote access writes three things and re-renders:

  - the edge's `site:` keys in the operator source (CADDY_TAILNET_HOSTNAME / _DOMAIN, CADDY_BIND),
    plus `edge` in an explicit `plugins:` list (`plugins: auto` enables it from the keys alone),
  - the Google OAuth client id + secret in secrets.env, and any internal secret the edge adds
    (its cookie secret, its gateway key), generated,
  - the SSO allowlist file oauth2-proxy mounts.

With the edge enabled the render drops the loopback UI ports: Caddy becomes the one front door.
Disabling removes the keys, the OAuth pair and the allowlist entries again. Every file is computed
before any is written, so invalid input or a refused source edit leaves everything untouched.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from . import wizard
from .catalog import Catalog
from .config import Source
from .plugins import PluginRegistry
from .render import RenderedConfig, render
from .source_edit import edit_plugins_list, edit_site_keys

REPO_ROOT = Path(__file__).resolve().parent.parent
# The allowlist oauth2-proxy mounts (services/edge/plugin.yaml) and its committed placeholder.
ALLOWLIST_PATH = REPO_ROOT / "auth" / "oauth2-proxy" / "emails.txt"
ALLOWLIST_PLACEHOLDER = "YOUR_ALLOWLIST_EMAIL"
# Where Caddy reads its TLS pair (auth/caddy/Caddyfile `tls_tailnet`, mounted from here).
CERT_DIR = REPO_ROOT / "auth" / "caddy" / "certs"

EDGE_PLUGIN = "edge"
OAUTH_CLIENT_KEYS = ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET")


def edge_site_keys(registry: PluginRegistry) -> tuple[str, ...]:
    """The site keys the edge cannot run without, read from its manifest (`requires.site`)."""
    edge = registry.get(EDGE_PLUGIN)
    if edge is None:
        raise ValueError(f"no '{EDGE_PLUGIN}' plugin in the registry")
    return edge.site_keys


def site_values(answers: wizard.RemoteAnswers) -> dict[str, str]:
    host = answers.hostname.strip().strip(".")
    return {"CADDY_BIND": answers.bind.strip(), "CADDY_TAILNET_HOSTNAME": host,
            "CADDY_TAILNET_DOMAIN": wizard.tailnet_domain(host)}


def tailscale_cert_argv(hostname: str) -> list[str]:
    """`tailscale cert` writing the pair to the file names Caddy loads (not `<host>.crt` in cwd)."""
    return ["tailscale", "cert",
            "--cert-file", (CERT_DIR / "tailnet.crt").as_posix(),
            "--key-file", (CERT_DIR / "tailnet.key").as_posix(),
            hostname]


@dataclasses.dataclass
class Change:
    """The outcome of enable/disable: the render of the new source, and what happened to secrets."""
    rendered: RenderedConfig
    generated_secret_keys: list[str]
    blank_secret_keys: list[str]


def _plugins_edit(text: str, action: str) -> str:
    plugins = (yaml.safe_load(text) or {}).get("plugins", "auto")
    if not isinstance(plugins, list):
        return text   # `auto` (or absent): the edge follows its site keys
    return edit_plugins_list(text, EDGE_PLUGIN, action)


def enable(source_path: Path, secrets_path: Path, answers: wizard.RemoteAnswers,
           catalog: Catalog, registry: PluginRegistry) -> Change:
    """Turn remote access on. Raises ValueError (nothing written) on invalid answers or a source
    the editors cannot change safely."""
    errors = wizard.remote_answer_errors(answers)
    if errors:
        raise ValueError("; ".join(errors))
    text = source_path.read_text(encoding="utf-8")
    new_text = _plugins_edit(edit_site_keys(text, site_values(answers), []), "add")
    rendered = render(Source.from_dict(yaml.safe_load(new_text)), catalog, registry)
    if EDGE_PLUGIN not in rendered.plugins_enabled:
        raise ValueError(f"the edge would still be off after this change: {'; '.join(rendered.warnings)}")
    source_path.write_text(new_text, encoding="utf-8")
    generated, blank = wizard.update_secrets(
        secrets_path, rendered.required_secrets,
        provided={"OAUTH2_PROXY_CLIENT_ID": answers.client_id, "OAUTH2_PROXY_CLIENT_SECRET": answers.client_secret})
    wizard.write_emails(answers.emails, ALLOWLIST_PATH)
    return Change(rendered, generated, [k for k in blank if k not in rendered.optional_secrets])


def disable(source_path: Path, secrets_path: Path, catalog: Catalog, registry: PluginRegistry) -> Change:
    """Turn remote access off: the edge's site keys, its entry in an explicit plugins list, the
    OAuth client pair and the allowlist entries go; the UIs publish their loopback ports again."""
    text = source_path.read_text(encoding="utf-8")
    new_text = _plugins_edit(edit_site_keys(text, {}, list(edge_site_keys(registry))), "remove")
    rendered = render(Source.from_dict(yaml.safe_load(new_text)), catalog, registry)
    source_path.write_text(new_text, encoding="utf-8")
    generated, blank = wizard.update_secrets(secrets_path, rendered.required_secrets,
                                             remove=list(OAUTH_CLIENT_KEYS))
    wizard.write_emails([ALLOWLIST_PLACEHOLDER], ALLOWLIST_PATH)
    return Change(rendered, generated, [k for k in blank if k not in rendered.optional_secrets])
