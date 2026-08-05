"""Cross-reference integrity: ids and doc links must resolve against reality.

Two rot classes the 2026-08-05 spring-clean audit found, now closed mechanically:

1. Every id registry (plugins, dashboards, agents) resolves an UNKNOWN id to a soft
   note + silent fallback — so a bad id in the tracked example config or the wizard's
   CAPABILITIES table "works" while quietly doing the wrong thing. Shipped instances:
   `ordo.example.yaml` said `dashboard: v2-native` (not a registered id — the prose
   name from ordo/dashboards.py won over the real id `native`), and wizard
   CAPABILITIES emitted the retired `worker` plugin id, silently dropped on render.

2. Nothing checked prose: docs accumulated links to deleted files (docs/architecture/,
   a gitignored superpowers spec) that no reader could follow.

Both tests are offline and filesystem-only.
"""
from __future__ import annotations

import re
import subprocess
import urllib.parse
from pathlib import Path

import yaml

from ordo.agents import AgentRegistry
from ordo.dashboards import DashboardRegistry
from ordo.plugins import PluginRegistry
from ordo.wizard import CAPABILITIES

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = PluginRegistry.load(ROOT / "services")
AGENTS = AgentRegistry.load(ROOT / "services")
DASHBOARDS = DashboardRegistry.load(ROOT / "services")


def _plugin_ids() -> set[str]:
    return {p.id for p in PLUGINS.plugins}


def test_example_yaml_ids_resolve_in_the_registries():
    """`ordo.example.yaml` is the tracked template every fresh install starts from; an
    unregistered id there silently falls back and every new deployment inherits the bug."""
    src = yaml.safe_load((ROOT / "ordo.example.yaml").read_text(encoding="utf-8"))

    dash = src.get("dashboard")
    if dash and dash != "auto":
        d, _notes = DASHBOARDS.resolve(dash)
        assert d is not None and d.id == dash, (
            f"ordo.example.yaml dashboard: {dash!r} is not a registered dashboard id "
            f"(registered: {sorted(x.id for x in DASHBOARDS.dashboards)})")

    agent = src.get("agent")
    if agent and agent != "auto":
        a, _notes = AGENTS.resolve(agent)
        assert a is not None and a.id == agent, (
            f"ordo.example.yaml agent: {agent!r} is not a registered agent id")

    plugins = src.get("plugins")
    if isinstance(plugins, list):
        unknown = set(plugins) - _plugin_ids()
        assert not unknown, f"ordo.example.yaml requests unregistered plugin(s): {sorted(unknown)}"


def test_wizard_capabilities_reference_only_registered_plugins():
    """The wizard writes these ids straight into a fresh install's plugins list; a dead id
    (e.g. the retired `worker`) is silently ignored at render, so the operator believes a
    capability is enabled when part of it no longer exists."""
    ids = _plugin_ids()
    for cap, meta in CAPABILITIES.items():
        unknown = set(meta.get("plugins", [])) - ids
        assert not unknown, (
            f"wizard CAPABILITIES[{cap!r}] names unregistered plugin(s): {sorted(unknown)} "
            f"— retired plugin left behind? (see the retirement-residue seam, audit 2026-08-05)")


# Inline links, including titled links ([x](path "title")) and paths containing spaces
# (this repo has a space-bearing docs dir) — both shapes the first regex missed entirely
# (QA audit 2026-08-05: a titled dead link was invisible to CI).
_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)]+?)(?:\s+\"[^\"]*\")?\s*\)")
_FENCED_CODE = re.compile(r"^```.*?^```", re.M | re.S)
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")  # http:, mailto:, vscode:, ftp:, …
# Historical records legitimately reference deleted files; runtime dirs are gitignored
# and absent for cloners by design.
_EXEMPT_SOURCES = {"CHANGELOG.md"}
_EXEMPT_TARGET_PREFIXES = ("data/", "out/", "models/")


def _tracked_markdown() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    # exists() guard: `git ls-files` reads the INDEX, which can briefly disagree with
    # the worktree (e.g. a deletion not yet staged). A file gone from disk has no links.
    return [ROOT / line for line in out.stdout.splitlines()
            if line and (ROOT / line).exists()]


def test_relative_markdown_links_resolve():
    """Every relative link in tracked markdown must point at a file that exists.
    External URLs and #anchors are out of scope; CHANGELOG is a historical record."""
    broken: list[str] = []
    for md in _tracked_markdown():
        rel_source = md.relative_to(ROOT).as_posix()
        if rel_source in _EXEMPT_SOURCES:
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        # Strip fenced code blocks: a doc teaching link syntax must not fail CI.
        text = _FENCED_CODE.sub("", text)
        for raw in _MD_LINK.findall(text):
            raw = raw.strip()
            # Any URI scheme (http, mailto, vscode, ftp, …) and protocol-relative
            # URLs are external — only repo paths are ours to validate.
            if raw.startswith(("#", "//")) or _HAS_SCHEME.match(raw):
                continue
            target = urllib.parse.unquote(raw.split("#", 1)[0].split("?", 1)[0])
            if not target:
                continue
            # Root-relative resolves from the repo root (GitHub rendering semantics),
            # relative from the doc's own directory.
            base = ROOT if target.startswith("/") else md.parent
            resolved = (base / target.lstrip("/")).resolve()
            try:
                rel_target = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                broken.append(f"{rel_source}: link escapes the repo: {raw}")
                continue
            if rel_target.startswith(_EXEMPT_TARGET_PREFIXES):
                continue
            if not resolved.exists():
                broken.append(f"{rel_source}: dead link -> {raw}")
    assert not broken, "dead markdown links:\n  " + "\n  ".join(broken)
