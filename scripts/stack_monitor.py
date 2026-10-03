#!/usr/bin/env python3
"""Ordo-AI-Stack image audit — "what is new upstream, and should we update?"

Enumerates EVERY service in the *deployed* compose (the rendered
`out/docker-compose.yml`) plus any extra compose stacks listed in the
STACK_AUDIT_SOURCES JSON file, classifies each image by how it is pinned,
resolves the latest upstream version where one exists, and collects the feature
notes of every release the deployed pin is missing. It emits a single JSON
document. The weekly cron injects that JSON into its prompt and the
`stack-audit` skill writes the Discord digest — the model curates, it does not
collect. Output is JSON by default (what the cron consumes); `--pretty` renders
a human-readable table for debugging.

Design notes / hard-won facts baked in as code (previously scattered across the
skill's reference files):
  - Deployed compose is `out/docker-compose.yml`, the only compose file (the root
    `docker-compose.yml` was removed on 2026-07-24). If it is missing the audit
    reports an error instead of auditing anything else.
  - `${VAR:-default}` image refs resolve against `.env` then the inline default.
  - Severity is install-aware: a CVE in release notes is only SECURITY if the
    pinned version is actually behind the fix. A bare `v` prefix is not a diff.
  - Pin kind drives the recommendation: semver→diffable, digest→manual bump,
    rolling→flag as drift every run, local build→rebuild-on-source-change.
  - No docker socket / no reliable git in the cron runtime, so we compare
    *declared* (what compose deploys) against *latest upstream*. Declared is the
    actionable surface; that is what an operator edits to update.

Report-only: this script never mutates compose or opens PRs.
Stdlib only (urllib). Per-source failure isolation, global deadline.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

STACK_ROOT = Path(os.environ.get("ORDO_STACK_ROOT", "/c/dev/ordo-ai-stack"))
SERVICES_DIR = STACK_ROOT / "services"
COMPOSE_FILE = STACK_ROOT / "out" / "docker-compose.yml"  # rendered = deployed
ENV_FILE = STACK_ROOT / "out" / ".env"
# Optional JSON file naming more compose stacks to audit next to Ordo's own:
#   {"stacks": [{"name": "media", "compose": "/path/docker-compose.yaml", "env": "/path/.env"}]}
# "env" is optional. Unset = audit Ordo only.
SOURCES_FILE = os.environ.get("STACK_AUDIT_SOURCES", "")
# GitHub token for the release API (5,000 requests/hour instead of 60). The Hermes runtime
# has no GITHUB_TOKEN, so fall back to the PAT file its git credential helper reads.
GITHUB_TOKEN_FILE = os.environ.get("GITHUB_BACKUP_PAT_FILE", "/run/secrets/github_backup_pat")
GLOBAL_DEADLINE_S = 100  # cron script timeout is 120s
FEATURE_WINDOW_DAYS = 7  # the cron runs weekly: releases newer than this are "new this week"
MAX_FEATURE_RELEASES = 5  # newest missed releases reported per service
MAX_FEATURES_PER_RELEASE = 5

_START = time.monotonic()
_INVISIBLE = dict.fromkeys(
    [0x200b, 0x200c, 0x200d, 0x200e, 0x200f, 0x2060, 0xfeff, 0x00ad,
     0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069],
    None,
)

# Per-service resolution hints, keyed by the registry-stripped image repo.
# 'gh' = GitHub owner/repo for release notes + semver; 'hub'/'quay' = registry
# repo for a tag-list fallback; 'upstream' = a different project to report as the
# real thing being tracked (e.g. the comfyui boot image tracks ComfyUI proper).
HINTS = {
    "caddy":                        {"gh": "caddyserver/caddy", "hub": "library/caddy"},
    "n8nio/n8n":                    {"gh": "n8n-io/n8n", "hub": "n8nio/n8n"},
    "open-webui/open-webui":        {"gh": "open-webui/open-webui", "hub": "openwebui/open-webui",
                                     "note": "versioned tags live on Docker Hub (docker.io/openwebui), not ghcr"},
    "qdrant/qdrant":                {"gh": "qdrant/qdrant", "hub": "qdrant/qdrant"},
    "oauth2-proxy/oauth2-proxy":    {"gh": "oauth2-proxy/oauth2-proxy", "quay": "oauth2-proxy/oauth2-proxy"},
    "grafana/grafana":              {"gh": "grafana/grafana", "hub": "grafana/grafana"},
    "prom/prometheus":              {"gh": "prometheus/prometheus", "hub": "prom/prometheus"},
    "searxng/searxng":              {"gh": "searxng/searxng", "hub": "searxng/searxng",
                                     "note": "rolling upstream — no semver releases; digest pin is correct"},
    "utkuozdemir/nvidia_gpu_exporter": {"gh": "utkuozdemir/nvidia_gpu_exporter"},
    "fedirz/faster-whisper-server": {"gh": "fedirz/faster-whisper-server", "hub": "fedirz/faster-whisper-server"},
    "remsky/kokoro-fastapi-gpu":    {"gh": "remsky/Kokoro-FastAPI"},
    "ggml-org/llama.cpp":           {"gh": "ggml-org/llama.cpp",
                                     "note": "moving tag; a catalog backend_image can name the patched build (ordo/llamacpp-patched)"},
    "yanwk/comfyui-boot":           {"hub": "yanwk/comfyui-boot", "upstream": ("ComfyUI", "comfy-org/ComfyUI"),
                                     "note": "boot wrapper; cu128-slim is a moving tag"},
    "prom/alertmanager":            {"gh": "prometheus/alertmanager", "hub": "prom/alertmanager"},
    "couchdb":                      {"hub": "library/couchdb"},
    "langfuse/langfuse":            {"gh": "langfuse/langfuse"},
    "langfuse/langfuse-worker":     {"gh": "langfuse/langfuse"},
    "tailscale/tailscale":          {"gh": "tailscale/tailscale", "hub": "tailscale/tailscale"},
    "clickhouse/clickhouse-server": {"gh": "ClickHouse/ClickHouse"},
    "isokoliuk/mcp-searxng":        {"gh": "ihor-sokoliuk/mcp-searxng"},
    "qmcgaw/gluetun":               {"gh": "qdm12/gluetun"},
    "linuxserver/prowlarr":         {"gh": "Prowlarr/Prowlarr"},
    "thephaseless/byparr":          {"gh": "ThePhaseless/Byparr"},
    "linuxserver/radarr":           {"gh": "Radarr/Radarr"},
    "linuxserver/sonarr":           {"gh": "Sonarr/Sonarr"},
    "linuxserver/bazarr":           {"gh": "morpheus65535/bazarr"},
    "recyclarr/recyclarr":          {"gh": "recyclarr/recyclarr"},
    "cloudflare/cloudflared":       {"gh": "cloudflare/cloudflared"},
    "seerr-team/seerr":             {"gh": "seerr-team/seerr"},
    "linuxserver/jellyfin":         {"gh": "jellyfin/jellyfin"},
    "cyfershepard/jellystat":       {"gh": "CyferShepard/Jellystat"},
    "schaka/janitorr":              {"gh": "Schaka/janitorr"},
    "gethomepage/homepage":         {"gh": "gethomepage/homepage"},
    "adguard/adguardhome":          {"gh": "AdguardTeam/AdGuardHome"},
    "vaultwarden/server":           {"gh": "dani-garcia/vaultwarden"},
    "infisical/infisical":          {"gh": "Infisical/infisical"},
}

# Registry namespaces that mean "built here", not pulled from a registry.
LOCAL_PREFIXES = ("ordo/",)  # every image `ordo build` makes, the patched llama.cpp build included
ROLLING_TAGS = {"latest", "stable", "main", "edge", "nightly", "dev",
                "server", "server-cuda", "cpu", "cu128-slim", "cu124-slim"}
BASE_IMAGE_RE = re.compile(r"^(python|alpine|ubuntu|debian|busybox|node|golang):", re.I)


# ── Utilities ────────────────────────────────────────────────────────────────

def budget_left() -> float:
    return GLOBAL_DEADLINE_S - (time.monotonic() - _START)


def scrub(text: str) -> str:
    return (text or "").translate(_INVISIBLE)


def http_json(url: str, headers=None, timeout: float = 15):
    timeout = max(3, min(timeout, budget_left()))
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "Ordo-AI-Stack-Monitor/4.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _read_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        return Path(GITHUB_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_github_token = _read_github_token()


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if _github_token:
        h["Authorization"] = f"token {_github_token}"
    return h


def github_json(path: str):
    """GET api.github.com/<path>. A rejected token (401: expired or revoked) is dropped for the
    rest of the run and the call retried anonymously, so a stale secret degrades to the
    60/hour anonymous limit instead of failing every lookup."""
    global _github_token
    url = f"https://api.github.com/{path}"
    try:
        return http_json(url, headers=gh_headers())
    except urllib.error.HTTPError as exc:
        if exc.code != 401 or not _github_token:
            raise
        _github_token = ""
        return http_json(url, headers=gh_headers())


# ── Version parsing ──────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def semver_tuple(tag: str):
    """Extract (major, minor, patch) from a tag, ignoring v/prefixes & -suffixes.
    Returns None if no numeric version is present (rolling/word tags)."""
    if not tag:
        return None
    tag = re.sub(r"^[a-zA-Z][\w.-]*@", "", tag)  # drop 'n8n@' style project prefix
    m = _SEMVER_RE.search(tag)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def is_prerelease(tag: str) -> bool:
    return bool(re.search(r"-(rc|beta|alpha|dev|pre|next)|\.rc\.|-rc\.", tag, re.I))


def compare(cur: str, latest: str):
    """Return (bucket, level); bucket in {major,minor,patch,same,unknown}."""
    c, lt = semver_tuple(cur), semver_tuple(latest)
    if c is None or lt is None:
        return "unknown", 0
    if lt <= c:
        return "same", 0  # equal to / ahead of upstream — not an update
    if lt[0] != c[0]:
        return "major", 3
    if lt[1] != c[1]:
        return "minor", 2
    return "patch", 1


# ── Compose parsing ──────────────────────────────────────────────────────────

def load_env(path: Path | None):
    env = {}
    if path is None:
        return env
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def resolve_ref(raw: str, env: dict) -> str:
    """Resolve a compose image string, expanding ${VAR} / ${VAR:-default}."""
    raw = raw.strip().strip('"').strip("'")
    m = re.match(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$", raw)
    if m:
        var, default = m.group(1), m.group(2)
        return env.get(var) or default or f"${{{var}}}"
    return raw


def parse_compose(path: Path, env: dict):
    """Return {service_name: image_ref}. Minimal hand-parse of service→image so
    the cron runtime needs no yaml dependency."""
    services = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return services
    in_services = False
    cur_service = None
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line):  # dedent to col 0 ends the block
            in_services = False
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9._-]+):\s*$", line)  # 2-space service header
        if m:
            cur_service = m.group(1)
            continue
        m = re.match(r"^\s+image:\s*(.+?)\s*$", line)
        if m and cur_service:
            services[cur_service] = resolve_ref(m.group(1), env)
    return {k: v for k, v in services.items() if v}


# ── Image classification ─────────────────────────────────────────────────────

def parse_image(ref: str):
    """Split an image ref into (registry, repo, tag, digest)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    registry = ""
    body = ref
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first):  # host[:port]/...
        registry, body = ref.split("/", 1)
    tag = ""
    if ":" in body.split("/")[-1]:
        body, tag = body.rsplit(":", 1)
    return registry, body, tag, digest


def classify(ref: str):
    registry, repo, tag, digest = parse_image(ref)
    if any(repo.startswith(p) or ref.startswith(p) for p in LOCAL_PREFIXES):
        return {"kind": "local_build", "repo": repo, "tag": tag or "latest"}
    if BASE_IMAGE_RE.match(ref):
        return {"kind": "base", "repo": repo, "tag": tag}
    if digest:
        return {"kind": "digest", "repo": repo, "tag": tag, "digest": digest[:19]}
    if tag and (tag.lower() in ROLLING_TAGS or semver_tuple(tag) is None):
        return {"kind": "rolling", "repo": repo, "tag": tag}
    return {"kind": "semver", "repo": repo, "tag": tag}


def hint_for(repo: str):
    for key, val in HINTS.items():
        if repo == key or repo.endswith("/" + key) or repo.split("/")[-1] == key:
            return val
    return {}


# ── Upstream latest resolution ───────────────────────────────────────────────

def github_latest(owner_repo: str):
    """(tag, url, body) of latest non-prerelease release, or (None, '', '')."""
    try:
        data = github_json(f"repos/{owner_repo}/releases/latest")
        if data.get("tag_name"):
            return data["tag_name"], data.get("html_url", ""), data.get("body", "") or ""
    except Exception:
        pass
    try:
        rels = github_json(f"repos/{owner_repo}/releases?per_page=15")
        for r in rels:
            if not r.get("prerelease") and not r.get("draft") and r.get("tag_name"):
                return r["tag_name"], r.get("html_url", ""), r.get("body", "") or ""
    except Exception:
        pass
    return None, "", ""


def dockerhub_latest_semver(repo: str):
    try:
        data = http_json(
            f"https://hub.docker.com/v2/repositories/{repo}/tags"
            f"?page_size=100&ordering=last_updated")
        best, best_name = None, None
        for t in data.get("results", []):
            name = t.get("name", "")
            if is_prerelease(name):
                continue
            sv = semver_tuple(name)
            if sv and (best is None or sv > best):
                best, best_name = sv, name
        return best_name
    except Exception:
        return None


def quay_latest_semver(repo: str):
    try:
        data = http_json(
            f"https://quay.io/api/v1/repository/{repo}/tag/?limit=100&onlyActiveTags=true")
        best, best_name = None, None
        for t in data.get("tags", []):
            name = t.get("name", "")
            if is_prerelease(name):
                continue
            sv = semver_tuple(name)
            if sv and (best is None or sv > best):
                best, best_name = sv, name
        return best_name
    except Exception:
        return None


def github_releases(owner_repo: str):
    """Published, non-prerelease releases, newest first (one API call)."""
    try:
        rels = github_json(f"repos/{owner_repo}/releases?per_page=40")
    except Exception:
        return []
    return [r for r in rels if not r.get("prerelease") and not r.get("draft") and r.get("tag_name")]


def newest_release(releases):
    """The highest-versioned release. Creation order is not enough: n8n publishes a release
    named `stable` alongside each version, and ClickHouse interleaves LTS backports."""
    versioned = [r for r in releases if semver_tuple(r["tag_name"]) is not None]
    if not versioned:
        return releases[0]
    return max(versioned, key=lambda r: semver_tuple(r["tag_name"]))


def resolve_latest(repo: str, hint: dict):
    """Return (latest_tag, url, body, releases). Prefer GitHub releases; fall back to a
    registry tag list so digest/rolling images still get a version to report.
    `releases` is the GitHub release list (empty for registry-only images)."""
    if "gh" in hint:
        releases = github_releases(hint["gh"])
        if releases:
            newest = newest_release(releases)
            return newest["tag_name"], newest.get("html_url", ""), newest.get("body") or "", releases
    if "hub" in hint:
        tag = dockerhub_latest_semver(hint["hub"])
        if tag:
            return tag, f"https://hub.docker.com/r/{hint['hub']}/tags", "", []
    if "quay" in hint:
        tag = quay_latest_semver(hint["quay"])
        if tag:
            return tag, f"https://quay.io/repository/{hint['quay']}?tab=tags", "", []
    return None, "", "", []


# ── Severity ─────────────────────────────────────────────────────────────────

_SECURITY_RE = re.compile(
    r"CVE-\d{4}-\d{3,}|vulnerabilit|exploit|buffer overflow|auth(?:entication)? bypass"
    r"|privilege escalation|\brce\b|remote code execution|security fix|security patch",
    re.I,
)


def severity(kind: str, cur: str, latest, body: str):
    """Return (tier, one-line reason). Tiers: SECURITY, UPDATE, DRIFT, REBUILD,
    OK, UNKNOWN."""
    if kind == "local_build":
        return "REBUILD", "built from repo — rebuild if source changed since deploy"
    if kind == "base":
        bucket, _ = compare(cur, latest) if latest else ("unknown", 0)
        if bucket in ("major", "minor", "patch"):
            return "UPDATE", f"base image {cur} → {latest}"
        return "OK", "base image current"
    if kind == "rolling":
        return "DRIFT", f"rolling tag ':{cur}' — unreproducible; latest upstream {latest or 'unknown'}"
    if latest is None:
        return "UNKNOWN", "could not resolve latest upstream"

    bucket, _ = compare(cur, latest)
    if kind == "digest":
        # A pure digest pin (no tag) can't be diffed against a version, so we
        # report the latest upstream for reference without claiming they match.
        if bucket in ("major", "minor", "patch"):
            base = f"digest-pinned; upstream now {latest} — manual bump"
            if body and _SECURITY_RE.search(body):
                return "SECURITY", f"upstream {latest} cites a security fix — review; " + base
            return "UPDATE", base
        if bucket == "same":
            return "OK", f"digest-pinned; tag {cur} is current"
        return "OK", f"digest-pinned; latest upstream is {latest} (bump manually if desired)"

    # semver
    if bucket == "same":
        return "OK", f"up to date ({cur})"
    if bucket == "unknown":
        return "UNKNOWN", f"version format unclear ({cur} vs {latest})"
    if body and _SECURITY_RE.search(body):
        return "SECURITY", f"{cur} → {latest} — release notes cite a security fix"
    return "UPDATE", f"{bucket} update {cur} → {latest}"


# ── Highlights ───────────────────────────────────────────────────────────────

def highlights(body: str, n: int = 3):
    out = []
    for line in (body or "").splitlines():
        s = line.strip().lstrip("-*• ").strip()
        if not s or s.startswith(("#", ">", "<!--", "|")):
            continue
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)      # md links → text
        s = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", s)
        s = re.sub(r"https?://\S+", "", s).strip()
        s = scrub(s)
        if len(s) > 12:
            out.append(s[:130])
        if len(out) >= n:
            break
    return out


# ── New features ─────────────────────────────────────────────────────────────
#
# The weekly digest answers "what major features shipped upstream that we do not run yet?".
# For a version pin that is every release newer than the pin. A rolling or bare-digest pin has
# no version to compare, so for those it is the releases published in the last week.

_FEATURE_HEADING_RE = re.compile(
    r"feature|highlight|what'?s new|\bnew\b|added|enhancement|improvement", re.I)
_NOISE_HEADING_RE = re.compile(
    r"contributor|dependenc|full changelog|checksum|docker image|fix|security", re.I)
# Outside a feature section only lines that read like a feature count, e.g. Sonarr's "New: ...",
# conventional-commit "feat: ...", or bazarr's "Added ...". Fix-only releases yield nothing.
_FEATURE_LINE_RE = re.compile(
    r"^(?:feat\b|feat\(|new\b|add(?:ed|s)?\b|supports?\b|introduc|enhance|allow|implement)", re.I)
_HEADING_RE = re.compile(r"^(?:#{1,6}\s+(.+?)|\*\*([^*]+)\*\*:?)\s*#*$")
_BULLET_RE = re.compile(r"^[-*\u2022]\s+")


def _clean_note(line: str) -> str:
    s = _BULLET_RE.sub("", line.strip())
    s = html.unescape(s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)          # md links -> text
    s = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", s)
    s = re.sub(r"\s+by @\S+(?:\s+in\s+\S+)?\s*$", "", s)      # GitHub "by @user in <PR url>"
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"^[0-9a-f]{7,40}\s+", "", s)                      # leading commit hash
    s = re.sub(r"(?:\s+-)?\s*\(?\b[0-9a-f]{7,40}\b\)?\s*$", "", s)  # trailing commit hash
    s = re.sub(r"\s*\(?(?:#\d+(?:,\s*)?)+\)?", "", s)           # "#1234" / "(#1234)" refs
    return scrub(s).strip()


def feature_lines(body: str, n: int = MAX_FEATURES_PER_RELEASE):
    """Bullet points from a release's feature sections, else bullets elsewhere (outside fix,
    security and contributor sections) that read like a feature. Fix-only notes give []."""
    section = "none"  # none = before any heading, then feature / noise / other
    featured, candidates = [], []
    for raw in (body or "").splitlines():
        line = raw.strip()
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(1) or heading.group(2)
            if _NOISE_HEADING_RE.search(title):
                section = "noise"
            elif _FEATURE_HEADING_RE.search(title):
                section = "feature"
            else:
                section = "other"
            continue
        if section == "noise" or not _BULLET_RE.match(line):
            continue
        text = _clean_note(line)
        if len(text) <= 12:
            continue
        if section == "feature":
            featured.append(text[:160])
        elif _FEATURE_LINE_RE.match(text):
            candidates.append(text[:160])
    return (featured or candidates)[:n]


def missed_releases(releases, declared: str, kind: str, now: datetime):
    """Releases the deployed pin does not have, newest first."""
    current = semver_tuple(declared) if kind in ("semver", "digest") else None
    cutoff = (now - timedelta(days=FEATURE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    missed = []
    for release in releases:
        if current is not None:
            version = semver_tuple(release.get("tag_name") or "")
            if version is None or version <= current:
                continue
        elif (release.get("published_at") or "")[:10] < cutoff:
            continue
        missed.append(release)
    return missed


def release_features(missed, now: datetime):
    """[{tag, published, new_this_week, url, features, summary}] for the newest missed releases.
    `summary` holds the opening lines of notes written as prose (no bullet list), e.g. Hermes."""
    cutoff = (now - timedelta(days=FEATURE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    out = []
    for release in missed[:MAX_FEATURE_RELEASES]:
        published = (release.get("published_at") or "")[:10]
        features = feature_lines(release.get("body") or "")
        out.append({
            "tag": release.get("tag_name"),
            "published": published,
            "new_this_week": published >= cutoff,
            "url": release.get("html_url", ""),
            "features": features,
            "summary": [] if features else highlights(release.get("body") or "", 2),
        })
    return out


# ── Pinned upstream sources ──────────────────────────────────────────────────
#
# The image table above answers "is this IMAGE behind?" — but a locally-built
# image (LOCAL_PREFIXES) is only ever REBUILD/"rebuild if source changed", and
# "source" there silently means TWO things: our Dockerfile, and the upstream repo
# that Dockerfile clones at a pinned SHA. The second was invisible.
#
# Live miss (2026-08-05): services/hermes/Dockerfile pinned
# HERMES_PINNED_SHA=5fdcfd85 (a 2026-05-19 commit) while upstream had shipped six
# releases carrying security fixes. Every audit reported `agent` as REBUILD and
# said nothing, because ordo/agent-hermes is a local build and nothing ever looked
# at NousResearch/hermes-agent. A pin that nobody watches is drift with extra steps.
#
# Discovery is by CONVENTION, not a per-service table: any services/*/Dockerfile
# declaring `ARG <X>_PINNED_SHA=` next to `ARG <X>_REPO=<github url>` is picked up
# automatically, so a future pinned build is covered without editing this file.

_ARG_RE = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)=(\S+)", re.M)
_GH_URL_RE = re.compile(r"github\.com[/:]([^/]+/[^/.\s]+?)(?:\.git)?$")


def discover_pinned_sources():
    """[{service, arg, sha, gh}] for every Dockerfile pinning an upstream git SHA."""
    out = []
    if not SERVICES_DIR.is_dir():
        return out
    for dockerfile in sorted(SERVICES_DIR.glob("*/Dockerfile")):
        try:
            text = dockerfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        args = {m.group(1): m.group(2).strip("\"'") for m in _ARG_RE.finditer(text)}
        for key, sha in args.items():
            if not key.endswith("_PINNED_SHA") or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                continue
            m = _GH_URL_RE.search(args.get(f"{key[:-len('_PINNED_SHA')]}_REPO", ""))
            if m:
                out.append({"service": dockerfile.parent.name, "arg": key,
                            "sha": sha, "gh": m.group(1)})
    return out


def github_commit_date(owner_repo: str, sha: str) -> str:
    """YYYY-MM-DD the pinned commit was authored, or '' if unresolvable."""
    try:
        data = github_json(f"repos/{owner_repo}/commits/{sha}")
        return ((data.get("commit") or {}).get("author") or {}).get("date", "")[:10]
    except Exception:
        return ""


def github_releases_after(owner_repo: str, iso_date: str):
    """Non-prerelease releases published after iso_date, newest first."""
    try:
        rels = github_json(f"repos/{owner_repo}/releases?per_page=30")
    except Exception:
        return []
    return [r for r in rels
            if not r.get("prerelease") and not r.get("draft")
            and (r.get("published_at") or "")[:10] > iso_date]


def audit_pinned_sources():
    """Report each pinned upstream source with the same tier vocabulary as images."""
    entries = []
    for src in discover_pinned_sources():
        row = {"stack": "ordo", "service": src["service"], "arg": src["arg"], "repo": src["gh"],
               "pinned_sha": src["sha"][:12], "pinned_date": "", "latest": None,
               "releases_behind": 0, "tier": "UNKNOWN", "reason": "", "url": "",
               "highlights": [], "new_features": []}
        if budget_left() < 12:
            row["reason"] = "skipped (time budget)"
            entries.append(row)
            continue
        latest, url, body = github_latest(src["gh"])
        row["latest"], row["url"] = latest, url
        row["pinned_date"] = github_commit_date(src["gh"], src["sha"])
        if not latest:
            row["reason"] = f"could not resolve latest release for {src['gh']}"
            entries.append(row)
            continue
        if not row["pinned_date"]:
            row["tier"] = "UNKNOWN"
            row["reason"] = f"pinned SHA {row['pinned_sha']} not found upstream (force-push? wrong repo?)"
            entries.append(row)
            continue
        missed = github_releases_after(src["gh"], row["pinned_date"])
        row["releases_behind"] = len(missed)
        row["new_features"] = release_features(missed, datetime.now(UTC))
        if not missed:
            row["tier"] = "OK"
            row["reason"] = f"pin ({row['pinned_date']}) is current with {latest}"
            entries.append(row)
            continue
        # Scan EVERY missed release for security language, not just the newest —
        # the fix that matters is often several releases back from HEAD.
        sec = next((r for r in missed if _SECURITY_RE.search(r.get("body") or "")), None)
        span = f"{len(missed)} release(s) behind — pinned {row['pinned_date']}, latest {latest}"
        if sec:
            row["tier"] = "SECURITY"
            row["reason"] = f"{sec.get('tag_name')} cites a security fix; {span}"
            row["highlights"] = highlights(sec.get("body") or "")
        else:
            row["tier"] = "UPDATE"
            row["reason"] = span
            row["highlights"] = highlights(body)
        entries.append(row)
    return entries


# ── Main ─────────────────────────────────────────────────────────────────────

def load_sources():
    """Compose stacks to audit: Ordo's deployed compose first, then STACK_AUDIT_SOURCES."""
    sources = [{"stack": "ordo", "compose": COMPOSE_FILE, "env": ENV_FILE}]
    if not SOURCES_FILE:
        return sources, []
    try:
        data = json.loads(Path(SOURCES_FILE).read_text(encoding="utf-8"))
        for entry in data["stacks"]:
            env = entry.get("env")
            sources.append({"stack": entry["name"], "compose": Path(entry["compose"]),
                            "env": Path(env) if env else None})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return sources, [f"sources file {SOURCES_FILE}: {type(exc).__name__}: {exc}"]
    return sources, []


def audit():
    if not COMPOSE_FILE.exists():
        return {"error": f"no compose file found at {COMPOSE_FILE} (run `ordo render` first)"}

    now = datetime.now(UTC)
    sources, failures = load_sources()
    services = []  # (stack, service, image ref)
    for src in sources:
        if not src["compose"].exists():
            failures.append(f"{src['stack']}: compose file not found at {src['compose']}")
            continue
        for name, ref in sorted(parse_compose(src["compose"], load_env(src["env"])).items()):
            services.append((src["stack"], name, ref))

    results = []
    resolved_cache = {}

    for stack, name, ref in services:
        info = classify(ref)
        kind, repo, tag = info["kind"], info["repo"], info.get("tag", "")
        hint = hint_for(repo)
        latest, url, body, releases = None, "", "", []

        if kind in ("semver", "digest", "rolling", "base"):
            if budget_left() < 8:
                failures.append(f"{stack}/{name}: skipped (time budget)")
            elif repo in resolved_cache:
                latest, url, body, releases = resolved_cache[repo]
            else:
                try:
                    if kind == "base":
                        latest = dockerhub_latest_semver(
                            repo if "/" in repo else f"library/{repo}")
                        url = f"https://hub.docker.com/_/{repo.split('/')[-1]}"
                    else:
                        latest, url, body, releases = resolve_latest(repo, hint)
                except Exception as e:  # noqa: BLE001
                    failures.append(f"{stack}/{name}: {type(e).__name__}: {e}")
                resolved_cache[repo] = (latest, url, body, releases)

        tracks = None
        if hint.get("upstream") and budget_left() > 8:
            up_name, up_repo = hint["upstream"]
            t, u, _ = github_latest(up_repo)
            if t:
                tracks = {"name": up_name, "latest": t, "url": u}

        tier, reason = severity(kind, tag, latest, body)
        missed = missed_releases(releases, tag, kind, now)
        bump, _ = compare(tag, latest) if latest else ("unknown", 0)
        results.append({
            "stack": stack,
            "service": name,
            "image": ref,
            "kind": kind,
            "declared": tag or (info.get("digest", "") + "…" if kind == "digest" else ""),
            "latest": latest,
            "bump": bump,
            "tier": tier,
            "reason": scrub(reason),
            "url": url,
            "highlights": highlights(body) if tier in ("UPDATE", "SECURITY") else [],
            "releases_behind": len(missed),
            "new_features": release_features(missed, now),
            "note": scrub(hint.get("note", "")),
            "tracks_upstream": tracks,
        })

    order = {"SECURITY": 0, "UPDATE": 1, "DRIFT": 2, "REBUILD": 3, "UNKNOWN": 4, "OK": 5}
    results.sort(key=lambda r: (order.get(r["tier"], 9), r["stack"], r["service"]))

    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    actionable = [r for r in results if r["tier"] in ("SECURITY", "UPDATE")]

    # Locally-built images are only ever REBUILD, so the upstream repo their
    # Dockerfile pins is audited separately — see the pinned-sources block above.
    # These count as actionable: a stale pin is exactly as real as a stale image.
    pinned = audit_pinned_sources()
    actionable += [p for p in pinned if p["tier"] in ("SECURITY", "UPDATE")]

    # 9p-wedge sweep: containers can report "healthy" while a worker sits in
    # unkillable D-state on the 9p bridge (live incident 2026-08-07). The cron
    # runtime has no docker socket, so ask the control plane's diagnostics endpoint;
    # unreachable is reported as a failure, never silently skipped.
    dstate = fetch_dstate()
    if dstate.get("error"):
        failures.append(f"dstate probe: {dstate['error']}")

    return {
        "date": now.strftime("%Y-%m-%d"),
        "dstate": dstate,
        "compose": str(COMPOSE_FILE),
        "stacks": [{"name": src["stack"], "compose": str(src["compose"])} for src in sources],
        "note": ("Audited the DEPLOYED compose. 'declared' = what compose ships; "
                 "compare against 'latest'. Tiers: SECURITY/UPDATE (act), "
                 "DRIFT (rolling/unpinned), REBUILD (local image), OK, UNKNOWN. "
                 "'pinned_sources' covers upstream repos that locally-built images "
                 "clone at a fixed SHA — a REBUILD image can still be months behind "
                 "upstream, which the image table alone cannot see. "
                 "'new_features' lists the feature notes of the newest releases the deployed "
                 "pin is missing (for rolling/bare-digest pins: releases from the last "
                 f"{FEATURE_WINDOW_DAYS} days); 'new_this_week' marks releases published in "
                 "that window. Report-only — no changes are applied."),
        "counts": counts,
        "actionable_count": len(actionable),
        "services": results,
        "pinned_sources": pinned,
        "meta": {"source_failures": failures, "service_count": len(services)},
    }


def fetch_dstate():
    """Query the control plane for D-state (wedged) processes. Same Bearer token Hermes'
    ops_client uses (OPS_CONTROLLER_TOKEN); OPS_CONTROLLER_URL overridable for tests."""
    url = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000") + "/diagnostics/dstate"
    token = os.environ.get("OPS_CONTROLLER_TOKEN", "")
    if not token:
        return {"error": "OPS_CONTROLLER_TOKEN not set in this runtime"}
    try:
        data = http_json(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"error": "unexpected dstate payload"}
    return data


def render_pretty(data):
    if "error" in data:
        return f"ERROR: {data['error']}"
    lines = [f"# Ordo-AI-Stack image audit — {data['date']}",
             f"compose: {data['compose']}",
             f"actionable: {data['actionable_count']}  counts: {data['counts']}", ""]
    ds = data.get("dstate") or {}
    if ds.get("p9_wedged"):
        lines.append("!! 9P WEDGE (D-state on the 9p bridge — healthchecks will NOT catch this;")
        lines.append("   FIRST check the container's logs: active progress = busy 9p reader, leave it;")
        lines.append("   no progress = wedged: restart the container, then the Docker VM if it won't die):")
        for w in ds["p9_wedged"]:
            lines.append(f"   {w['container']}: pid {w['pid']} {w['comm']} wchan={w['wchan']}")
        lines.append("")
    elif ds.get("wedged"):
        lines.append("D-state processes (non-9p — informational, re-check next run): "
                     + ", ".join(f"{w['container']}:{w['comm']}" for w in ds["wedged"]))
        lines.append("")
    for r in data["services"]:
        lines.append(f"[{r['tier']:8}] {r['stack'] + '/' + r['service']:32} {(r['declared'] or r['kind']):>18}"
                     f" -> {str(r['latest'] or '-'):<14} {r['reason']}")
        for rel in r["new_features"]:
            mark = "*" if rel["new_this_week"] else " "
            lines.append(f"    {mark} {rel['tag']} ({rel['published']}): " + " | ".join(rel["features"]))
    if data.get("pinned_sources"):
        lines.append("\npinned upstream sources (locally-built images):")
        for p in data["pinned_sources"]:
            lines.append(f"[{p['tier']:8}] {p['service']:24} {p['pinned_sha']:>18}"
                         f" -> {str(p['latest'] or '-'):<14} {p['reason']}")
    if data["meta"]["source_failures"]:
        lines.append("\nfailures: " + "; ".join(data["meta"]["source_failures"]))
    return "\n".join(lines)


def main():
    pretty = "--pretty" in sys.argv
    data = audit()
    if pretty:
        sys.stdout.write(render_pretty(data) + "\n")
    else:
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return 0 if "error" not in data else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
