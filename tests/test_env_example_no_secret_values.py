"""Lint: .env.example must be feature-flags-only.

Real secrets belong in secrets/.env.sops (compose-substituted values) or
secrets/<name>.sops (file-form tokens consumed via _FILE bridges). This
test fails the build if .env.example contains a key whose name matches a
secret pattern AND has a populated value (any non-empty RHS).

Documented safe defaults are whitelisted explicitly. To add a new
intentional value, extend SAFE_DEFAULTS below with a comment explaining why
it is safe to ship a literal value in a public template.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Variable-name suffixes / fragments that signal "this is a secret".
SECRET_PATTERNS = (
    re.compile(r"_TOKEN$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_PASSWORD$"),
    re.compile(r"_KEY$"),
    re.compile(r"_API_KEY$"),
    re.compile(r"_ACCESS_TOKEN$"),
    re.compile(r"^DISCORD_TOKEN$"),
    re.compile(r"^DASHBOARD_AUTH_TOKEN$"),
    re.compile(r"^OPS_CONTROLLER_TOKEN$"),
    re.compile(r"^OAUTH2_PROXY_CLIENT_SECRET$"),
    re.compile(r"^OAUTH2_PROXY_COOKIE_SECRET$"),
    re.compile(r"^SEARXNG_SECRET$"),
    re.compile(r"^N8N_OWNER_PASSWORD$"),
    re.compile(r"^HF_TOKEN$"),
    re.compile(r"^CIVITAI_TOKEN$"),
    re.compile(r"^github_personal_access_token$", re.IGNORECASE),
)

# Names that match SECRET_PATTERNS but whose .env.example value is
# documented as safe (a known default, a placeholder, a path pointer, etc.).
# Add a comment explaining why each entry is here.
SAFE_DEFAULTS: dict[str, set[str]] = {
    # LiteLLM master key default is the literal string "local" — used only
    # for the in-stack proxy; not a real credential.
    "LITELLM_MASTER_KEY": {"local"},
    # *_FILE values point at docker-secret mount paths inside containers,
    # not at credentials. Listed for clarity even though the name suffix is
    # not in SECRET_PATTERNS.
}


def _scan_env_example():
    """Yield (lineno, key, value) for every uncommented `KEY=VALUE` line."""
    for lineno, raw in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            yield lineno, m.group(1), m.group(2).strip()


def test_env_example_secret_keys_have_no_values():
    """Any variable with a secret-looking name in .env.example must have
    an empty value. Real values belong in secrets/.env.sops or
    secrets/<name>.sops, both encrypted via SOPS+age."""
    violations = []
    for lineno, key, value in _scan_env_example():
        if not value:
            continue
        if not any(pat.search(key) for pat in SECRET_PATTERNS):
            continue
        allowed = SAFE_DEFAULTS.get(key, set())
        if value in allowed:
            continue
        violations.append(
            f"{ENV_EXAMPLE.name}:{lineno}: {key} has a populated value "
            f"({value[:20]}{'…' if len(value) > 20 else ''}) — move it to "
            f"secrets/.env.sops (compose-substituted) or "
            f"secrets/{key.lower()}.sops (file-form), and leave the "
            f"placeholder in .env.example empty."
        )
    assert not violations, "Secrets must not ship populated in .env.example:\n  " + "\n  ".join(violations)
