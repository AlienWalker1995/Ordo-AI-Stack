#!/usr/bin/env python3
"""Ordo-AI-Stack image audit — "should we update this image?"

Enumerates EVERY service in the *deployed* compose (the rendered
`out/docker-compose.yml`, not the root file), classifies each image by how it
is pinned, resolves the latest upstream version where one exists, and emits a
single JSON document. The daily cron injects that JSON into its prompt and the
`stack-audit` skill writes the Discord digest — the model curates, it does not
collect. Output is JSON by default (what the cron consumes); `--pretty` renders
a human-readable table for debugging.

Design notes / hard-won facts baked in as code (previously scattered across the
skill's reference files):
  - Deployed compose is `out/docker-compose.yml`; the root `docker-compose.yml`
    is a different, stale file. Auditing the wrong one was the original bug.
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

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

STACK_ROOT = Path(os.environ.get("ORDO_STACK_ROOT", "/c/dev/ordo-ai-stack"))
COMPOSE_CANDIDATES = [
    STACK_ROOT / "v2" / "out" / "docker-compose.yml",  # rendered = deployed
    STACK_ROOT / "docker-compose.yml",                 # fallback
]
ENV_FILE = STACK_ROOT / ".env"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
GLOBAL_DEADLINE_S = 100  # cron script timeout is 120s

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
                                     "note": "moving tag; prod runs a locally-patched build (see .env LLAMACPP_IMAGE)"},
    "yanwk/comfyui-boot":           {"hub": "yanwk/comfyui-boot", "upstream": ("ComfyUI", "comfy-org/ComfyUI"),
                                     "note": "boot wrapper; cu128-slim is a moving tag"},
}

# Registry namespaces that mean "built here", not pulled from a registry.
LOCAL_PREFIXES = ("ordo/", "ordo-v2/", "ordo-ai-stack-", "ordo-ai-stack/")  # ordo/ = current; rest historical
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


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"token {GITHUB_TOKEN}"
    return h


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

def load_env():
    env = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
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
        data = http_json(f"https://api.github.com/repos/{owner_repo}/releases/latest",
                          headers=gh_headers())
        if data.get("tag_name"):
            return data["tag_name"], data.get("html_url", ""), data.get("body", "") or ""
    except Exception:
        pass
    try:
        rels = http_json(f"https://api.github.com/repos/{owner_repo}/releases?per_page=15",
                         headers=gh_headers())
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


def resolve_latest(repo: str, hint: dict):
    """Return (latest_tag, url, body). Prefer GitHub releases; fall back to a
    registry tag list so digest/rolling images still get a version to report."""
    if "gh" in hint:
        tag, url, body = github_latest(hint["gh"])
        if tag:
            return tag, url, body
    if "hub" in hint:
        tag = dockerhub_latest_semver(hint["hub"])
        if tag:
            return tag, f"https://hub.docker.com/r/{hint['hub']}/tags", ""
    if "quay" in hint:
        tag = quay_latest_semver(hint["quay"])
        if tag:
            return tag, f"https://quay.io/repository/{hint['quay']}?tab=tags", ""
    return None, "", ""


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


# ── Main ─────────────────────────────────────────────────────────────────────

def audit():
    env = load_env()
    compose_path = next((p for p in COMPOSE_CANDIDATES if p.exists()), None)
    if compose_path is None:
        return {"error": f"no compose file found (tried {[str(p) for p in COMPOSE_CANDIDATES]})"}

    services = parse_compose(compose_path, env)
    results = []
    failures = []
    resolved_cache = {}

    for name, ref in sorted(services.items()):
        info = classify(ref)
        kind, repo, tag = info["kind"], info["repo"], info.get("tag", "")
        hint = hint_for(repo)
        latest, url, body = None, "", ""

        if kind in ("semver", "digest", "rolling", "base"):
            if budget_left() < 8:
                failures.append(f"{name}: skipped (time budget)")
            elif repo in resolved_cache:
                latest, url, body = resolved_cache[repo]
            else:
                try:
                    if kind == "base":
                        latest = dockerhub_latest_semver(
                            repo if "/" in repo else f"library/{repo}")
                        url = f"https://hub.docker.com/_/{repo.split('/')[-1]}"
                    else:
                        latest, url, body = resolve_latest(repo, hint)
                except Exception as e:  # noqa: BLE001
                    failures.append(f"{name}: {type(e).__name__}: {e}")
                resolved_cache[repo] = (latest, url, body)

        tracks = None
        if hint.get("upstream") and budget_left() > 8:
            up_name, up_repo = hint["upstream"]
            t, u, _ = github_latest(up_repo)
            if t:
                tracks = {"name": up_name, "latest": t, "url": u}

        tier, reason = severity(kind, tag, latest, body)
        results.append({
            "service": name,
            "image": ref,
            "kind": kind,
            "declared": tag or (info.get("digest", "") + "…" if kind == "digest" else ""),
            "latest": latest,
            "tier": tier,
            "reason": scrub(reason),
            "url": url,
            "highlights": highlights(body) if tier in ("UPDATE", "SECURITY") else [],
            "note": scrub(hint.get("note", "")),
            "tracks_upstream": tracks,
        })

    order = {"SECURITY": 0, "UPDATE": 1, "DRIFT": 2, "REBUILD": 3, "UNKNOWN": 4, "OK": 5}
    results.sort(key=lambda r: (order.get(r["tier"], 9), r["service"]))

    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    actionable = [r for r in results if r["tier"] in ("SECURITY", "UPDATE")]

    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "compose": str(compose_path),
        "note": ("Audited the DEPLOYED compose. 'declared' = what compose ships; "
                 "compare against 'latest'. Tiers: SECURITY/UPDATE (act), "
                 "DRIFT (rolling/unpinned), REBUILD (local image), OK, UNKNOWN. "
                 "Report-only — no changes are applied."),
        "counts": counts,
        "actionable_count": len(actionable),
        "services": results,
        "meta": {"source_failures": failures, "service_count": len(services)},
    }


def render_pretty(data):
    if "error" in data:
        return f"ERROR: {data['error']}"
    lines = [f"# Ordo-AI-Stack image audit — {data['date']}",
             f"compose: {data['compose']}",
             f"actionable: {data['actionable_count']}  counts: {data['counts']}", ""]
    for r in data["services"]:
        lines.append(f"[{r['tier']:8}] {r['service']:24} {(r['declared'] or r['kind']):>18}"
                     f" -> {str(r['latest'] or '-'):<14} {r['reason']}")
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
