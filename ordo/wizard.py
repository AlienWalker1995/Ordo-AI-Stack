"""Guided setup wizard — the front end of the one-command install.

Takes a fresh operator from nothing to a written config: detect hardware → confirm the
auto-picked model → choose capabilities → tailnet + Google SSO + access → generate/collect
secrets → write ``ordo.yaml`` (the declarative source) and ``secrets.env`` (operator secrets).
Everything downstream renders from ``ordo.yaml``; ``secrets.env`` is read by compose as a
second env_file and NEVER committed.

The *logic* (plan / build_source / capability + secret mapping) is deliberately separated from
*I/O* (prompts + file writes) so it is testable without a TTY — that separation is also how a
headless/CI install works: feed an ``answers`` dict, get a valid source + a secrets set.
"""
from __future__ import annotations

import base64
import dataclasses
import os
import secrets as _secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .catalog import Catalog
from .config import Source
from .hardware import HardwareProfile, detect
from .plugins import PluginRegistry
from .render import render

# ── Capabilities → plugin ids ────────────────────────────────────────────────
# Chat (llama.cpp + model-gateway + Open WebUI + Hermes + the MCP gateway) is ALWAYS on — it is
# the core stack, not a toggle. The optional capability groups below each map to the plugin ids
# that provide them. The wizard builds an explicit `plugins:` list as "everything auto would
# enable, MINUS the plugin ids of the capabilities the operator turned off" — robust because it
# never has to re-enumerate the always-on baseline (edge, tailnet-names, dashboards, memory
# tools, …); those simply stay in. Hardware gating still applies at render time.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "image-video": {
        "label": "Image + video generation (ComfyUI, LTX-2)",
        "plugins": ["comfyui", "comfyui-mcp", "song-gen", "worker"],
        "gpu": True,
    },
    "rag": {
        "label": "RAG / retrieval (Qdrant + embeddings)",
        "plugins": ["rag", "qdrant-rag"],
        "gpu": False,
    },
    "voice": {
        "label": "Voice (STT + TTS) — needs a second GPU",
        "plugins": ["voice"],
        "gpu": True,
    },
    "automation": {
        "label": "Automation (n8n workflows)",
        "plugins": ["automation", "n8n"],
        "gpu": False,
    },
    "search": {
        "label": "Web search (self-hosted SearXNG)",
        "plugins": ["searxng-web", "searxng"],
        "gpu": False,
    },
    "monitoring": {
        "label": "Monitoring (Grafana + Prometheus + GPU exporter)",
        "plugins": ["monitoring"],
        "gpu": False,
    },
}


# ── Secrets: what the wizard generates vs. prompts for ────────────────────────
# GENERATED keys are internal shared secrets with no external authority — the wizard mints a
# strong random value so the operator never has to. Everything else in a render's
# `required_secrets` is EXTERNAL (issued by Google / Hugging Face / Tailscale / GitHub) and is
# prompted for (skippable — left blank in secrets.env for the operator to fill later).
def _cookie_secret() -> str:
    # oauth2-proxy requires a cookie secret of EXACTLY 16, 24, or 32 bytes (AES-SIV); a urlsafe
    # base64 of 32 random bytes decodes back to 32 bytes and is what oauth2-proxy's docs recommend.
    return base64.urlsafe_b64encode(_secrets.token_bytes(32)).decode("ascii")


SECRET_GENERATORS: dict[str, Any] = {
    "LITELLM_MASTER_KEY": lambda: "sk-" + _secrets.token_hex(24),
    "OPS_CONTROLLER_TOKEN": lambda: _secrets.token_urlsafe(32),
    "MCP_GATEWAY_TOKEN": lambda: _secrets.token_urlsafe(32),
    "OAUTH2_PROXY_COOKIE_SECRET": _cookie_secret,
    "SEARXNG_SECRET": lambda: _secrets.token_hex(32),
    "N8N_API_KEY": lambda: _secrets.token_urlsafe(32),
}

# External secrets — human-readable prompt text (order = display order). A key that is required
# by the render but absent here AND not in SECRET_GENERATORS is still emitted (blank) so nothing
# the stack needs is silently dropped.
EXTERNAL_SECRETS: dict[str, str] = {
    "OAUTH2_PROXY_CLIENT_ID": "Google OAuth client ID",
    "OAUTH2_PROXY_CLIENT_SECRET": "Google OAuth client secret",
    "HF_TOKEN": "Hugging Face token (gated model pulls) — optional, Enter to skip",
    "TS_AUTHKEY": "Tailscale auth key (clean tailnet service URLs) — optional, Enter to skip",
    "GITHUB_PERSONAL_ACCESS_TOKEN": "GitHub PAT (GitHub MCP + ComfyUI-Manager) — optional, Enter to skip",
}


@dataclasses.dataclass
class WizardPlan:
    """What the wizard would propose, for the user to confirm/override."""
    hardware: HardwareProfile
    tier: str
    model_id: str
    model_name: str
    ctx_estimate: int
    plugins_available: list[str]
    warnings: list[str]


def plan(catalog: Catalog, registry: PluginRegistry,
         hardware: HardwareProfile | None = None) -> WizardPlan:
    hw = hardware or detect()
    model, warns = catalog.best_fit(hw)
    available, notes = registry.resolve("auto", hw)
    return WizardPlan(
        hardware=hw, tier=model.tier, model_id=model.id, model_name=model.name,
        ctx_estimate=model.ctx_default, plugins_available=[p.id for p in available],
        warnings=warns + notes,
    )


def _tailnet_domain(hostname: str) -> str:
    """`ordo.tail1234.ts.net` → `tail1234.ts.net` (everything after the first label)."""
    host = (hostname or "").strip().strip(".")
    return host.split(".", 1)[1] if "." in host else host


def plugins_from_capabilities(enabled_caps: list[str] | None,
                              all_plugin_ids: list[str]) -> Any:
    """Turn the set of ENABLED optional capabilities into an ordo.yaml `plugins:` value.

    All capabilities on (or None) → ``"auto"`` (let the render enable everything the hardware
    supports). Otherwise → an explicit list = every registry plugin MINUS the plugin ids of the
    capabilities the operator turned off. Always-on/baseline plugins (not owned by any capability)
    are never removed, so a partial selection can't accidentally drop the core stack.
    """
    if enabled_caps is None:
        return "auto"
    enabled = set(enabled_caps)
    if enabled >= set(CAPABILITIES):  # every optional capability kept → same as auto
        return "auto"
    drop: set[str] = set()
    for cap, meta in CAPABILITIES.items():
        if cap not in enabled:
            drop |= set(meta["plugins"])
    return [pid for pid in all_plugin_ids if pid not in drop]


def build_source(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn wizard answers into a valid ordo.yaml dict. All fields optional → sane defaults.

    answers keys (all optional):
        hardware ('auto'|spec), tier, model, agent, dashboard,
        plugins ('auto'|list) — the RESOLVED plugin selection,
        cloud_fallback (dict), overrides (dict),
        caddy_hostname, caddy_domain, caddy_bind — edge access, folded into `site:`,
        site (dict) — extra verbatim env keys, merged under the caddy_* ones.
    """
    a = answers or {}

    site: dict[str, Any] = dict(a.get("site", {}) or {})
    host = str(a.get("caddy_hostname", "") or "").strip()
    if host:
        site["CADDY_TAILNET_HOSTNAME"] = host
        site["CADDY_TAILNET_DOMAIN"] = str(a.get("caddy_domain") or _tailnet_domain(host))
    bind = str(a.get("caddy_bind", "") or "").strip()
    if bind:
        site["CADDY_BIND"] = bind

    src: dict[str, Any] = {
        "hardware": a.get("hardware", "auto"),
        "tier": a.get("tier", "auto"),
        "model": a.get("model", "auto"),
        "agent": a.get("agent", "hermes"),        # Hermes is the default
        "dashboard": a.get("dashboard", "native"),
        "plugins": a.get("plugins", "auto"),
        "cloud_fallback": a.get("cloud_fallback", {"enabled": False}),
        "overrides": a.get("overrides", {}),
    }
    if site:
        src["site"] = site
    return src


def write_source(source: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Generated by `ordo init`. This is the single source of truth — edit it and\n"
              "# re-run `ordo render`. Editing rendered outputs does nothing (they regenerate).\n")
    p.write_text(header + yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
    return p


def resolve_secrets(required_keys: list[str],
                    provided: dict[str, str] | None = None) -> tuple[dict[str, str], list[str], list[str], list[str]]:
    """Map a render's required secret KEYS to values.

    Returns (values, generated_keys, provided_keys, blank_keys):
      * generated — internal shared secrets minted with `secrets.token_*` (always non-empty)
      * provided  — external secrets the operator supplied (from `provided`)
      * blank     — required-but-unfilled external keys (emitted empty in secrets.env)
    Every required key appears in `values` exactly once (no drift from the render's set).
    """
    provided = provided or {}
    values: dict[str, str] = {}
    generated: list[str] = []
    given: list[str] = []
    blank: list[str] = []
    for key in required_keys:
        supplied = str(provided.get(key, "") or "").strip()
        if supplied:
            values[key] = supplied
            given.append(key)
        elif key in SECRET_GENERATORS:
            values[key] = SECRET_GENERATORS[key]()
            generated.append(key)
        else:
            values[key] = ""
            blank.append(key)
    return values, generated, given, blank


def write_secrets(values: dict[str, str], path: str | Path) -> Path:
    """Write secrets.env (chmod 600). Values are real secrets — this file is gitignored and
    NEVER committed. Only the KEY set mirrors the render's secrets.env.example."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# GENERATED by `ordo init` — real secret VALUES (NEVER commit this file).",
        "# Internal secrets were auto-generated; external ones (Google/HF/Tailscale/GitHub)",
        "# you provided or left blank to fill in later. Regenerate keys via `ordo render` ->",
        "# secrets.env.example; this file is read by compose as a second env_file.",
    ]
    lines += [f"{k}={v}" for k, v in values.items()]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # best-effort; a no-op ACL-wise on Windows but harmless
    except OSError:
        pass
    return p


def write_emails(emails: list[str], path: str | Path) -> Path:
    """Write the oauth2-proxy allowlist (one email per line). This is a TRACKED repo file the
    edge mounts read-only; only written when the operator supplies at least one address."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = [e.strip() for e in emails if e.strip()]
    p.write_text("\n".join(clean) + "\n", encoding="utf-8")
    return p


@dataclasses.dataclass
class WizardResult:
    """Structured outcome — the CLI turns this into the post-wizard render/fetch/up offers."""
    source_path: Path
    secrets_path: Path
    emails_path: Path | None
    ordo_yaml: dict[str, Any]
    required_secrets: list[str]
    generated_secret_keys: list[str]
    provided_secret_keys: list[str]
    blank_secret_keys: list[str]
    compose_profiles: list[str]
    caddy_hostname: str
    caddy_bind: str
    warnings: list[str]


# ── I/O helpers (interactive only) ───────────────────────────────────────────
def _prompt(msg: str, default: str = "") -> str:  # pragma: no cover - interactive only
    suffix = f" [{default}]" if default else ""
    ans = input(f"{msg}{suffix}: ").strip()
    return ans or default


def _confirm(msg: str, default: bool = True) -> bool:  # pragma: no cover - interactive only
    d = "Y/n" if default else "y/N"
    ans = input(f"{msg} [{d}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _tailscale_ip() -> str:  # pragma: no cover - shells to tailscale
    if not shutil.which("tailscale"):
        return ""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _try_tailscale_cert(hostname: str) -> None:  # pragma: no cover - shells to tailscale
    if not (hostname and shutil.which("tailscale")):
        if hostname:
            print("  tailscale not on PATH — skip cert; issue it later with "
                  f"`tailscale cert {hostname}`")
        return
    if _confirm(f"Run `tailscale cert {hostname}` now to issue the edge TLS cert?", default=False):
        try:
            subprocess.run(["tailscale", "cert", hostname], check=False)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  cert issuance failed ({e}) — run `tailscale cert {hostname}` manually later")


def _collect_answers(catalog: Catalog, registry: PluginRegistry,
                     pl: WizardPlan) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    # pragma: no cover below (interactive)  — every branch here is TTY-driven.
    a: dict[str, Any] = {}
    all_ids = [p.id for p in registry.plugins]

    # 1. Hardware
    print(f"\nDetected hardware: {pl.hardware.summary()}")
    if not _confirm("Use detected hardware?", default=True):
        print("  Pin hardware later by editing `hardware:` in ordo.yaml (CI/reproducibility).")

    # 2. Model
    print(f"\nRecommended model: {pl.model_name}  (tier={pl.tier}, ~{pl.ctx_estimate:,} ctx)")
    for w in pl.warnings:
        print(f"  ! {w}")
    if not _confirm("Accept the recommended model?", default=True):
        by_tier: dict[str, list[str]] = {}
        for m in catalog.models:
            by_tier.setdefault(m.tier, []).append(m.id)
        for tier, ids in by_tier.items():
            print(f"    [{tier}] {', '.join(ids)}")
        a["model"] = _prompt("Model id (or 'auto')", "auto")

    # 3. Capabilities
    print("\nCapabilities (chat is always on). Enter accepts the default hardware-gated set.")
    if _confirm("Customize which optional capabilities are enabled?", default=False):
        enabled_caps: list[str] = []
        for cap, meta in CAPABILITIES.items():
            if _confirm(f"  enable {meta['label']}?", default=not meta["gpu"] or pl.hardware.has_gpu):
                enabled_caps.append(cap)
        a["plugins"] = plugins_from_capabilities(enabled_caps, all_ids)
    else:
        a["plugins"] = "auto"

    # 4. Access (tailnet + bind)
    print("\nEdge access (Caddy front door + Google SSO).")
    host = _prompt("Tailnet hostname (e.g. ordo.tail1234.ts.net)", "")
    if host:
        a["caddy_hostname"] = host
    ts_ip = _tailscale_ip()
    bind_default = ts_ip or "0.0.0.0"
    hint = f" (tailnet IP {ts_ip})" if ts_ip else ""
    bind = _prompt(f"Caddy bind address — tailnet IP restricts to the tailnet, 0.0.0.0 = all "
                   f"interfaces{hint}", bind_default)
    a["caddy_bind"] = bind
    if host:
        _try_tailscale_cert(host)

    # 5. Google SSO
    provided: dict[str, str] = {}
    emails: list[str] = []
    print("\nGoogle SSO — create an OAuth 2.0 Client (type: Web application) at")
    print(f"  https://console.cloud.google.com/apis/credentials  · Authorized redirect URI = "
          f"https://{host or '<hostname>'}/oauth2/callback")
    provided["OAUTH2_PROXY_CLIENT_ID"] = _prompt("Google OAuth client ID", "")
    provided["OAUTH2_PROXY_CLIENT_SECRET"] = _prompt("Google OAuth client secret", "")
    raw = _prompt("Allowlisted emails (comma-separated)", "")
    emails = [e for e in (x.strip() for x in raw.split(",")) if e]

    # 6. External tokens
    print("\nExternal tokens (optional — Enter to skip any):")
    for key, label in EXTERNAL_SECRETS.items():
        if key in provided:  # OAUTH2_* already collected above
            continue
        provided[key] = _prompt(f"  {label}", "")

    return a, provided, emails


def run(catalog: Catalog, registry: PluginRegistry, out_dir: str | Path,
        interactive: bool = True, answers: dict[str, Any] | None = None,
        emails_path: str | Path | None = None) -> WizardResult:
    """Run the wizard. Non-interactive (`interactive=False`) is the headless/CI path: it consumes
    `answers` (and `answers['secrets']` / `answers['emails']`) and writes config only.

    Writes ``<out_dir>/ordo.yaml`` and ``<out_dir>/secrets.env``; optionally the oauth2-proxy
    allowlist at ``emails_path``. Returns a WizardResult describing what was written.
    """
    out = Path(out_dir)
    pl = plan(catalog, registry)

    if interactive:  # pragma: no cover - TTY-driven
        a, provided, emails = _collect_answers(catalog, registry, pl)
    else:
        a = dict(answers or {})
        provided = dict(a.pop("secrets", {}) or {})
        emails = list(a.pop("emails", []) or [])

    source = build_source(a)
    source_path = write_source(source, out / "ordo.yaml")

    # Render in-memory (writes NOTHING) purely to learn the exact secret KEY set the selected
    # stack needs + its compose profiles — data-driven, so the wizard never hardcodes a key list.
    rc = render(Source.from_dict(source), catalog, registry)
    values, gen, given, blank = resolve_secrets(rc.required_secrets, provided)
    secrets_path = write_secrets(values, out / "secrets.env")

    emails_written: Path | None = None
    if emails and emails_path is not None:
        emails_written = write_emails(emails, emails_path)

    return WizardResult(
        source_path=source_path, secrets_path=secrets_path, emails_path=emails_written,
        ordo_yaml=source, required_secrets=rc.required_secrets,
        generated_secret_keys=gen, provided_secret_keys=given, blank_secret_keys=blank,
        compose_profiles=rc.compose_profiles,
        caddy_hostname=str(source.get("site", {}).get("CADDY_TAILNET_HOSTNAME", "")),
        caddy_bind=str(source.get("site", {}).get("CADDY_BIND", "")),
        warnings=rc.warnings,
    )
