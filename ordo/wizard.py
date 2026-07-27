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
import re
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


class SetupCancelled(Exception):
    """Raised when the operator aborts the interactive wizard (Ctrl-C, EOF, or a declined
    confirmation). Nothing has been written when this propagates — the caller reports a clean
    cancel and exits non-zero."""

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
    "notes": {
        "label": "Notes sync (Obsidian / CouchDB LiveSync, cross-device)",
        "plugins": ["obsidian-livesync"],
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
    # Obsidian notes sync (CouchDB LiveSync). token_urlsafe is base64url — JSON-safe for the
    # bridge's generated config, and shell-safe. The E2EE passphrase encrypts note content at rest
    # in CouchDB; the operator enters the SAME value in every Obsidian LiveSync client.
    "COUCHDB_PASSWORD": lambda: _secrets.token_urlsafe(24),
    "LIVESYNC_E2EE_PASSPHRASE": lambda: _secrets.token_urlsafe(32),
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
    header = ("# Generated by `ordo init`. This is the single source of truth - edit it and\n"
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


# ── Input validation (pure — unit-tested without a TTY) ──────────────────────
def hostname_error(s: str) -> str | None:
    """Return a human error string if `s` isn't a plausible tailnet DNS hostname, else None."""
    s = (s or "").strip()
    if not s:
        return "hostname is empty"
    if any(c in s for c in " \t/:"):
        return "no scheme, port, or spaces — just the name, e.g. ordo.tail1234.ts.net"
    if "." not in s:
        return "expected a fully-qualified name like ordo.tail1234.ts.net"
    return None


def parse_emails(raw: str) -> list[str]:
    """Split a comma/space/newline-separated string into cleaned, de-duplicated addresses
    (input order preserved)."""
    out: list[str] = []
    for part in re.split(r"[,\s]+", (raw or "").strip()):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def invalid_emails(emails: list[str]) -> list[str]:
    """Return the subset of `emails` that don't look like `local@domain.tld`."""
    return [e for e in emails if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e)]


# ── I/O helpers (interactive only) ───────────────────────────────────────────
def _read(prompt: str) -> str:  # pragma: no cover - interactive only
    """input() that turns Ctrl-C / end-of-input into a clean SetupCancelled."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SetupCancelled from None


def _confirm(msg: str, default: bool = True) -> bool:  # pragma: no cover - interactive only
    d = "Y/n" if default else "y/N"
    ans = _read(f"{msg} [{d}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _prompt(msg: str, default: str = "", *, required: bool = False,
            validate: Any = None, why: str = "") -> str:  # pragma: no cover - interactive only
    """Prompt for a value.

    * `validate(value) -> error|None` rejects malformed input with a re-prompt.
    * `required=True`: a blank answer is NOT silently accepted — the operator is asked whether to
      leave it blank and configure later (y) or re-enter (n); Ctrl-C cancels the whole setup.
      `why` explains what breaks if it's left blank.
    """
    suffix = f" [{default}]" if default else ""
    while True:
        ans = _read(f"{msg}{suffix}: ").strip() or default
        if ans and validate is not None:
            err = validate(ans)
            if err:
                print(f"  ! {err}")
                continue
        if ans or not required:
            return ans
        tail = f" ({why})" if why else ""
        if _confirm(f"  leave blank and configure later?{tail}", default=False):
            return ""
        # else: re-prompt (Ctrl-C to abort the wizard)


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


_STEPS = 6


def _welcome() -> None:  # pragma: no cover - interactive only
    bar = "=" * 60
    print(f"\n{bar}")
    print("  Ordo setup")
    print("  Configure your whole stack in a few minutes. Every choice")
    print("  has a sensible default (press Enter to accept it).")
    print("")
    print("  Before you start, have these ready for the front-door step:")
    print("    * a Tailscale tailnet hostname (e.g. ordo.tail1234.ts.net)")
    print("    * a Google OAuth 2.0 Web client (id + secret)")
    print("  You can skip the front door and add it later.")
    print("")
    print("  Press Ctrl-C at any prompt to cancel. Nothing is written")
    print("  until you review and confirm at the end.")
    print(f"{bar}")


def _collect_answers(catalog: Catalog, registry: PluginRegistry, pl: WizardPlan,
                     out_dir: Path) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    # pragma: no cover below (interactive)  — every branch here is TTY-driven.
    a: dict[str, Any] = {}
    all_ids = [p.id for p in registry.plugins]
    _welcome()

    # Step 1 - Hardware
    print(f"\nStep 1/{_STEPS} - Hardware")
    print(f"  detected: {pl.hardware.summary()}")
    if not _confirm("Use detected hardware?", default=True):
        print("  -> left on auto-detect; pin it later via `hardware:` in ordo.yaml.")

    # Step 2 - Model
    print(f"\nStep 2/{_STEPS} - Model")
    print(f"  best fit: {pl.model_name}  (tier={pl.tier}, ~{pl.ctx_estimate:,} ctx)")
    for w in pl.warnings:
        print(f"  ! {w}")
    model_label = f"{pl.model_name} (auto)"
    if not _confirm("Accept the recommended model?", default=True):
        by_tier: dict[str, list[str]] = {}
        for m in catalog.models:
            by_tier.setdefault(m.tier, []).append(m.id)
        for tier, ids in by_tier.items():
            print(f"    [{tier}] {', '.join(ids)}")
        chosen = _prompt("Model id (or 'auto')", "auto")
        a["model"] = chosen
        model_label = chosen

    # Step 3 - Capabilities
    print(f"\nStep 3/{_STEPS} - Capabilities  (chat is always on)")
    caps_label = "auto (everything the hardware supports)"
    if _confirm("Customize which optional capabilities are enabled?", default=False):
        enabled_caps: list[str] = []
        for cap, meta in CAPABILITIES.items():
            if _confirm(f"  enable {meta['label']}?", default=not meta["gpu"] or pl.hardware.has_gpu):
                enabled_caps.append(cap)
        a["plugins"] = plugins_from_capabilities(enabled_caps, all_ids)
        caps_label = ", ".join(enabled_caps) or "chat only"
    else:
        a["plugins"] = "auto"

    # Step 4 - Secure front door (Tailscale + Google SSO). Required inputs are enforced here:
    # a blank hostname / client id / client secret / allowlist prompts to leave-blank-or-retry,
    # so the operator makes an explicit choice instead of silently shipping a broken SSO gate.
    provided: dict[str, str] = {}
    emails: list[str] = []
    print(f"\nStep 4/{_STEPS} - Secure front door (Tailscale + Google SSO)")
    print("  Gates every UI behind Google sign-in on your tailnet.")
    if _confirm("Set up the secure front door now?", default=True):
        host = _prompt("Tailnet hostname (e.g. ordo.tail1234.ts.net)", "",
                       required=True, validate=hostname_error,
                       why="the SSO front door can't come up without it")
        if host:
            a["caddy_hostname"] = host
        callback = f"https://{host or '<hostname>'}/oauth2/callback"
        print("  Create an OAuth 2.0 Client at https://console.cloud.google.com/apis/credentials")
        print(f"    type: Web application   Authorized redirect URI: {callback}")
        provided["OAUTH2_PROXY_CLIENT_ID"] = _prompt(
            "Google OAuth client ID", "", required=True, why="required for Google sign-in")
        provided["OAUTH2_PROXY_CLIENT_SECRET"] = _prompt(
            "Google OAuth client secret", "", required=True, why="required for Google sign-in")
        while True:
            raw = _prompt("Allowlisted emails (comma-separated)", "", required=True,
                          why="no one can sign in until an email is allowlisted")
            emails = parse_emails(raw)
            bad = invalid_emails(emails)
            if not bad:
                break
            print(f"  ! not a valid email: {', '.join(bad)}")
        ts_ip = _tailscale_ip()
        hint = f" (tailnet IP {ts_ip})" if ts_ip else ""
        a["caddy_bind"] = _prompt(
            f"Caddy bind address - tailnet IP restricts to the tailnet, 0.0.0.0 = all{hint}",
            ts_ip or "0.0.0.0")
        if host:
            _try_tailscale_cert(host)
        frontdoor_label = f"on - {host}" if host else "on - (hostname deferred)"
    else:
        print("  ! Skipping SSO: the stack will run WITHOUT the sign-in gate. Anyone who can reach")
        print("    the bind address gets unauthenticated access. Bind to loopback to stay safe,")
        print("    or add SSO later with `ordo init --force`.")
        if not _confirm("Continue without the SSO front door?", default=False):
            raise SetupCancelled
        a["caddy_bind"] = _prompt(
            "Caddy bind address (127.0.0.1 = this machine only, recommended without SSO)",
            "127.0.0.1")
        frontdoor_label = "OFF (no SSO gate)"

    # Step 5 - External tokens (all optional)
    print(f"\nStep 5/{_STEPS} - External tokens  (all optional - Enter to skip)")
    for key, label in EXTERNAL_SECRETS.items():
        if key in provided:  # OAUTH2_* already collected in the front-door step
            continue
        provided[key] = _prompt(f"  {label}", "")

    # Step 6 - Review & confirm. NOTHING is written until this is accepted.
    have = [k for k, v in provided.items() if v]
    blank = [k for k, v in provided.items() if not v]
    print(f"\nStep 6/{_STEPS} - Review")
    print(f"  hardware      {pl.hardware.summary()}")
    print(f"  model         {model_label}")
    print(f"  capabilities  {caps_label}")
    print(f"  front door    {frontdoor_label}")
    if emails:
        print(f"  allowlist     {', '.join(emails)}")
    secret_line = f"{len(SECRET_GENERATORS)} auto-generated"
    if have:
        secret_line += f"; provided: {', '.join(have)}"
    if blank:
        secret_line += f"; blank: {', '.join(blank)}"
    print(f"  secrets       {secret_line}")
    print(f"  writes        {out_dir / 'ordo.yaml'}  +  {out_dir / 'secrets.env'}")
    if not _confirm("\nWrite this configuration?", default=True):
        raise SetupCancelled

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
        a, provided, emails = _collect_answers(catalog, registry, pl, out)
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
