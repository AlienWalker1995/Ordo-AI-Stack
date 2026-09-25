"""Guided setup wizard — the front end of the one-command install.

Takes a fresh operator from nothing to a written config in at most three questions: confirm the
auto-picked model for the detected hardware → pick a feature preset → (in the CLI) start now.
Internal secrets are generated; nothing asks for an account. It writes ``ordo.yaml`` (the
declarative source) and the operator secrets: into the SOPS file ``site: SECRETS_SOURCE`` names,
materialized to ``secrets.env``, or (no SOPS file configured) into ``secrets.env`` itself (see
``ordo/host/secret_store.py``). Remote access (Tailscale + Google SSO) is a later opt-in: ``ordo remote
enable`` reuses the prompts and validators defined here. Everything downstream renders from
``ordo.yaml``; compose interpolates each service's declared secrets from ``secrets.env``
(``--env-file``), which is NEVER committed.

The *logic* (plan / build_source / capability + secret mapping) is deliberately separated from
*I/O* (prompts + file writes) so it is testable without a TTY — that separation is also how a
headless/CI install works: feed an ``answers`` dict, get a valid source + a secrets set.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import render
from ..render.hardware import HardwareProfile, detect
from ..render.plugins import PluginRegistry
from . import secret_store
from .secret_store import generator_for


class SetupCancelled(Exception):
    """Raised when the operator aborts the interactive wizard (Ctrl-C, EOF, or a declined
    confirmation). Nothing has been written when this propagates — the caller reports a clean
    cancel and exits non-zero."""

# ── Capabilities → plugin ids ────────────────────────────────────────────────
# Chat (llama.cpp + model-gateway incl. its MCP gateway + Open WebUI + Hermes) is ALWAYS on - it is
# the core stack, not a toggle. The optional capability groups below each map to the plugin ids
# that provide them. The wizard builds an explicit `plugins:` list as "everything auto would
# enable, MINUS the plugin ids of the capabilities the operator turned off" — robust because it
# never has to re-enumerate the always-on baseline (edge, tailnet-names, dashboards, memory
# tools, …); those simply stay in. Hardware gating still applies at render time.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "image-video": {
        "label": "Image + video generation (ComfyUI, LTX-2)",
        "plugins": ["comfyui", "comfyui-mcp", "song-gen"],
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

# The one features question: each preset keeps a set of the optional capabilities above
# (None = everything the hardware supports, i.e. `plugins: auto`). Chat itself is always on; the
# chat preset keeps `rag` because Open WebUI stores its documents in Qdrant and cannot run without it.
FEATURE_PRESETS: dict[str, dict[str, Any]] = {
    "chat": {"label": "Chat only (Open WebUI + the agent)", "capabilities": ["rag"]},
    "tools": {"label": "Chat + tools (web search, document RAG, n8n automation)",
              "capabilities": ["rag", "search", "automation"]},
    "everything": {"label": "Everything this hardware supports", "capabilities": None},
}
DEFAULT_FEATURES = "everything"


def plugins_from_features(preset: str, all_plugin_ids: list[str]) -> Any:
    """The ordo.yaml `plugins:` value for a feature preset ("auto" for everything)."""
    if preset not in FEATURE_PRESETS:
        raise ValueError(f"unknown feature preset {preset!r} (choose one of {', '.join(FEATURE_PRESETS)})")
    return plugins_from_capabilities(FEATURE_PRESETS[preset]["capabilities"], all_plugin_ids)


@dataclasses.dataclass
class WizardPlan:
    """What the wizard would propose, for the user to confirm/override."""
    hardware: HardwareProfile
    tier: str
    model_id: str
    model_name: str
    ctx_estimate: int
    warnings: list[str]


def plan(catalog: Catalog, registry: PluginRegistry,
         hardware: HardwareProfile | None = None) -> WizardPlan:
    hw = hardware or detect()
    model, warns = catalog.best_fit(hw)
    _, notes = registry.resolve("auto", hw)
    return WizardPlan(
        hardware=hw, tier=model.tier, model_id=model.id, model_name=model.name,
        ctx_estimate=model.ctx_default, warnings=warns + notes,
    )


def tailnet_domain(hostname: str) -> str:
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


def _drop_plugins_missing_site_keys(plugin_ids: list[str], registry: PluginRegistry,
                                    site: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Keep an explicit plugins list renderable: leave out each plugin whose required site keys
    are unset (the render refuses an explicit list naming one), with a note saying how to add it."""
    kept: list[str] = []
    notes: list[str] = []
    for plugin_id in plugin_ids:
        plugin = registry.get(plugin_id)
        missing = plugin.missing_site_keys(site) if plugin else []
        if missing:
            notes.append(f"'{plugin_id}' left out of plugins: set {', '.join(missing)} under `site:` in "
                         f"ordo.yaml and add '{plugin_id}' to `plugins:`, then re-run `ordo render`")
        else:
            kept.append(plugin_id)
    return kept, notes


def _remote_access_plugins(registry: PluginRegistry) -> set[str]:
    """The edge plugin and every plugin that (transitively) depends on it."""
    found = {"edge"}
    changed = True
    while changed:
        changed = False
        for plugin in registry.plugins:
            if plugin.id not in found and found & set(plugin.depends_on):
                found.add(plugin.id)
                changed = True
    return found


def build_source(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn wizard answers into a valid ordo.yaml dict. All fields optional → sane defaults.

    answers keys (all optional):
        hardware ('auto'|spec), tier, model, agent, dashboard,
        plugins ('auto'|list) — the RESOLVED plugin selection,
        overrides (dict),
        site (dict): verbatim env keys. Remote access (the edge's CADDY_* keys) is written by
        `ordo remote enable`, not here.
    """
    a = answers or {}

    site: dict[str, Any] = dict(a.get("site", {}) or {})

    src: dict[str, Any] = {
        "hardware": a.get("hardware", "auto"),
        "tier": a.get("tier", "auto"),
        "model": a.get("model", "auto"),
        "agent": a.get("agent", "hermes"),        # Hermes is the default
        "dashboard": a.get("dashboard", "dashboard"),
        "plugins": a.get("plugins", "auto"),
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
        elif (gen := generator_for(key)) is not None:
            values[key] = gen()
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
        "# secrets.env.example; compose interpolates each service's declared keys from it.",
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
    """Structured outcome: the CLI prints what was chosen and turns this into the "start now" offer."""
    source_path: Path
    secrets_path: Path
    generated_secret_keys: list[str]
    blank_secret_keys: list[str]
    compose_profiles: list[str]
    warnings: list[str]
    model_id: str = ""
    model_name: str = ""
    plugins_enabled: list[str] = dataclasses.field(default_factory=list)   # service plugins
    mcp_servers: list[str] = dataclasses.field(default_factory=list)       # tool servers
    optional_blank_secret_keys: list[str] = dataclasses.field(default_factory=list)
    secrets_store: str = ""                # where the values live: the SOPS file, or secrets.env itself


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


def _choose(msg: str, options: dict[str, str], default: str) -> str:  # pragma: no cover - interactive
    """Numbered single choice; returns the chosen option's key (Enter keeps `default`)."""
    keys = list(options)
    for number, key in enumerate(keys, start=1):
        marker = "  (default)" if key == default else ""
        print(f"    [{number}] {options[key]}{marker}")
    while True:
        ans = _read(f"{msg} [{keys.index(default) + 1}]: ").strip()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(keys):
            return keys[int(ans) - 1]
        print(f"  ! enter a number from 1 to {len(keys)}")


def _welcome() -> None:  # pragma: no cover - interactive only
    bar = "=" * 60
    print(f"\n{bar}")
    print("  Ordo setup: three questions, each with a default (press Enter).")
    print("  Everything runs on this machine; no accounts needed.")
    print("  Remote access (Tailscale + Google sign-in) is a later,")
    print("  optional step: `ordo remote enable`.")
    print("  Press Ctrl-C to cancel; nothing is written before the end.")
    print(f"{bar}")


def _collect_answers(catalog: Catalog, registry: PluginRegistry, pl: WizardPlan,
                     out_dir: Path) -> dict[str, Any]:
    # pragma: no cover below (interactive) - every branch here is TTY-driven.
    # Two of the local path's three questions live here (model, features); the third, "start
    # now?", is asked by `ordo init` after the config is written.
    a: dict[str, Any] = {}
    # Opt-in plugins (`default: false`) are excluded here for the same reason `plugins: auto`
    # excludes them: the features question never offers them, so it must not enable them either.
    all_ids = [p.id for p in registry.plugins if p.default]
    _welcome()

    print(f"\nDetected: {pl.hardware.summary()}")
    print(f"Best-fit model: {pl.model_name}  (tier={pl.tier}, ~{pl.ctx_estimate:,} ctx)")
    for w in pl.warnings:
        print(f"  ! {w}")
    if not _confirm("1/3  Use this model?", default=True):
        by_tier: dict[str, list[str]] = {}
        for m in catalog.models:
            by_tier.setdefault(m.tier, []).append(m.id)
        for tier, ids in by_tier.items():
            print(f"    [{tier}] {', '.join(ids)}")
        a["model"] = _read("  Model id (Enter = auto): ").strip() or "auto"

    print("")
    options = {key: meta["label"] for key, meta in FEATURE_PRESETS.items()}
    preset = _choose("2/3  Features", options, DEFAULT_FEATURES)
    a["plugins"] = plugins_from_features(preset, all_ids)
    return a


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


@dataclasses.dataclass
class RemoteAnswers:
    """What `ordo remote enable` collects: the tailnet name, the Caddy bind, the Google OAuth
    client and the allowlist. The domain is derived from the hostname."""
    hostname: str
    bind: str
    client_id: str
    client_secret: str
    emails: list[str]


def remote_answer_errors(answers: RemoteAnswers) -> list[str]:
    """Every problem with a set of remote-access answers (empty list = usable). Never echoes the
    client secret."""
    errors = []
    host_err = hostname_error(answers.hostname)
    if host_err:
        errors.append(f"hostname: {host_err}")
    if not answers.bind.strip():
        errors.append("bind: an address is required (the tailnet IP, or 0.0.0.0)")
    if not answers.client_id.strip():
        errors.append("client id: required (Google OAuth 2.0 Web client)")
    if not answers.client_secret.strip():
        errors.append("client secret: required (Google OAuth 2.0 Web client)")
    if not answers.emails:
        errors.append("emails: at least one allowlisted address is required")
    bad = invalid_emails(answers.emails)
    if bad:
        errors.append(f"emails: not a valid address: {', '.join(bad)}")
    return errors


def ask_remote_access() -> RemoteAnswers:  # pragma: no cover - interactive only
    """The remote-access prompts (`ordo remote enable`). Each required value re-prompts until it
    is valid; Ctrl-C cancels with nothing written."""
    print("\nRemote access: Tailscale HTTPS + Google sign-in in front of every UI.")
    print("  Needs: MagicDNS + HTTPS certificates on in your tailnet, and a Google OAuth client.")
    host = _prompt("Tailnet hostname (e.g. ordo.tail1234.ts.net)", "", validate=hostname_error)
    while not host:
        host = _prompt("Tailnet hostname (required)", "", validate=hostname_error)
    ts_ip = _tailscale_ip()
    hint = f" (tailnet IP {ts_ip})" if ts_ip else ""
    bind = _prompt(f"Caddy bind address - tailnet IP restricts to the tailnet, 0.0.0.0 = all{hint}",
                   ts_ip or "0.0.0.0")
    print("  Create an OAuth 2.0 Client at https://console.cloud.google.com/apis/credentials")
    print(f"    type: Web application   Authorized redirect URI: https://{host}/oauth2/callback")
    client_id = ""
    while not client_id:
        client_id = _prompt("Google OAuth client ID", "")
    client_secret = ""
    while not client_secret:
        client_secret = _prompt("Google OAuth client secret", "")
    while True:
        emails = parse_emails(_prompt("Allowlisted emails (comma-separated)", ""))
        bad = invalid_emails(emails)
        if emails and not bad:
            break
        print(f"  ! not a valid email: {', '.join(bad)}" if bad else "  ! at least one email is required")
    return RemoteAnswers(hostname=host, bind=bind, client_id=client_id, client_secret=client_secret,
                         emails=emails)


def run(catalog: Catalog, registry: PluginRegistry, out_dir: str | Path,
        interactive: bool = True, answers: dict[str, Any] | None = None,
        host_root: str | Path | None = None, secrets_source: str | None = None) -> WizardResult:
    """Run the wizard. Non-interactive (`interactive=False`) is the headless/CI path: it consumes
    `answers` (`features` picks a preset, `secrets` supplies values) and writes config only.

    Writes ``<out_dir>/ordo.yaml`` and ``<out_dir>/secrets.env`` (materialized from the SOPS file when
    one is configured). Returns a WizardResult
    describing what was written and chosen.

    ``secrets_source`` (``site: SECRETS_SOURCE``) names a SOPS file to keep the secrets in; without
    it ``secrets.env`` is the store.

    ``host_root`` is the repo checkout on the host. Every host bind is ``${BASE_PATH:?}`` /
    ``${DATA_PATH:?}`` (fail loud: a relative path resolves to a host path that does not exist
    when ops-controller recreates a service), so the source records both unless the operator
    already chose them, plus a MEMORY_VAULT_PATH under DATA_PATH.
    """
    out = Path(out_dir)
    pl = plan(catalog, registry)

    provided: dict[str, str] = {}
    if interactive:  # pragma: no cover - TTY-driven
        a = _collect_answers(catalog, registry, pl, out)
    else:
        a = dict(answers or {})
        provided = dict(a.pop("secrets", {}) or {})
        features = a.pop("features", None)
        if features is not None:
            a["plugins"] = plugins_from_features(str(features), [p.id for p in registry.plugins if p.default])

    if host_root is not None:
        site = dict(a.get("site") or {})
        base = Path(host_root).resolve().as_posix()
        site.setdefault("BASE_PATH", base)
        site.setdefault("DATA_PATH", f"{site['BASE_PATH']}/data")
        # memory-vault requires a vault path; a local vault under data/ is the sensible default.
        # The edge stays off until the operator supplies its CADDY_* keys.
        site.setdefault("MEMORY_VAULT_PATH", f"{site['DATA_PATH']}/memory-vault")
        a["site"] = site

    if secrets_source:
        # The operator keeps secrets in a SOPS file (a private repo): record it, so every later writer
        # edits that file and materializes secrets.env from it.
        a["site"] = {**dict(a.get("site") or {}), "SECRETS_SOURCE": secrets_source}

    source = build_source(a)
    site_notes: list[str] = []
    if isinstance(source["plugins"], list):
        source["plugins"], site_notes = _drop_plugins_missing_site_keys(
            source["plugins"], registry, source.get("site", {}))
    source_path = write_source(source, out / "ordo.yaml")

    # Render in-memory (writes NOTHING) purely to learn the exact secret KEY set the selected
    # stack needs + its compose profiles — data-driven, so the wizard never hardcodes a key list.
    rc = render(Source.from_dict(source), catalog, registry)
    store = secret_store.store_for(source.get("site") or {}, out,
                                   repo_root=Path(host_root) if host_root is not None else Path.cwd())
    if store.materializes:
        # The private repo's SOPS file (or an Infisical project) is the store: add what it lacks (an
        # existing value is never replaced, so a re-init keeps every generate-once secret), then
        # materialize out/secrets.env.
        gen, blank = secret_store.update(store, rc.required_secrets, provided)
        secrets_path = secret_store.materialize(store, secret_store.SecretNeeds.from_render(rc), out,
                                                strict=False).secrets_env
    else:
        values, gen, _given, blank = resolve_secrets(rc.required_secrets, provided)
        secrets_path = write_secrets(values, out / "secrets.env")
        secret_store.materialize(store, secret_store.SecretNeeds.from_render(rc), out, strict=False)

    # The edge and what depends on it are off because remote access is off: that is the local
    # install working as intended, and `ordo remote enable` (which the CLI points to) turns it on.
    # Their "set CADDY_* under site:" notes would only send a local user to hand-edit the source.
    remote_only = _remote_access_plugins(registry)
    notes = [w for w in site_notes + rc.warnings if not any(w.startswith(f"'{pid}' ") for pid in remote_only)]

    return WizardResult(
        source_path=source_path, secrets_path=secrets_path, secrets_store=store.description,
        generated_secret_keys=gen,
        blank_secret_keys=[k for k in blank if k not in rc.optional_secrets],
        optional_blank_secret_keys=[k for k in blank if k in rc.optional_secrets],
        compose_profiles=rc.compose_profiles,
        warnings=notes,
        model_id=rc.model.id, model_name=rc.model.name, plugins_enabled=list(rc.plugins_enabled),
        mcp_servers=[str(server["id"]) for server in rc.mcp_servers],
    )
