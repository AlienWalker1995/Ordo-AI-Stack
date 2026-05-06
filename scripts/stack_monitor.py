#!/usr/bin/env python3
"""
Ordo-AI-Stack — Package Audit & Update Manager
══════════════════════════════════════════════════
Comprehensive monitor that:
  1. Checks ALL services in docker-compose.yml against their latest releases
  2. Classifies severity: CRITICAL (security), HIGH (major), MEDIUM (minor), LOW (patch)
  3. Outputs structured JSON for the cron job to consume
  4. Can also APPLY updates if called with --apply

Usage:
  python3 stack_monitor.py              # Audit only, outputs JSON to stdout
  python3 stack_monitor.py --apply      # Audit + apply approved updates (see APPROVED_UPDATES)
  python3 stack_monitor.py --json       # JSON output to stdout
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = STACK_ROOT / "docker-compose.yml"
MONITOR = STACK_ROOT / "data" / "hermes" / "scripts" / "github_monitor.py"
HERMES_DOCKERFILE = STACK_ROOT / "hermes" / "Dockerfile"

# All services to monitor (sources of truth).
#
# pin_source: "compose" (default) reads the version string from docker-compose.yml.
#             "dockerfile" reads HERMES_PINNED_SHA from hermes/Dockerfile and
#                          compares SHAs against the upstream tag.
SERVICES = {
    # GitHub-backed (API releases)
    "n8n":         {"repo": "n8n-io/n8n",        "compose_key": "n8n",         "type": "github"},
    "Open WebUI":  {"repo": "open-webui/open-webui", "compose_key": "open-webui", "type": "github"},
    "Qdrant":      {"repo": "qdrant/qdrant",     "compose_key": "qdrant",      "type": "github"},
    "Caddy":       {"repo": "caddyserver/caddy", "compose_key": "caddy",       "type": "github"},
    "llama.cpp":   {"repo": "ggml-org/llama.cpp", "compose_key": "llamacpp-embed", "type": "github"},
    "LiteLLM":     {"repo": "BerriAI/litellm",   "compose_key": None,          "type": "github"},  # Docker-only
    "ComfyUI":     {"repo": "Comfy-Org/ComfyUI", "compose_key": None,          "type": "github"},  # Managed via comfyui-boot
    # Docker images without GitHub releases
    "ComfyUI-Manager": {"repo": "ltdrdata/ComfyUI-Manager", "compose_key": None, "type": "atom"},
    "ComfyUI-KJNodes":   {"repo": "kijai/ComfyUI-KJNodes",  "compose_key": None, "type": "atom"},
    "ComfyUI-VideoHelperSuite": {"repo": "Kosinkadink/ComfyUI-VideoHelperSuite", "compose_key": None, "type": "atom"},
    "oauth2-proxy":  {"repo": "oauth2-proxy/oauth2-proxy", "compose_key": "oauth2-proxy", "type": "github"},
    # Source-built image — pinned by SHA in hermes/Dockerfile, not in docker-compose.yml.
    "Hermes Agent":  {"repo": "NousResearch/hermes-agent", "compose_key": None, "type": "github",
                      "pin_source": "dockerfile"},
}

# Current pinned versions (synced from docker-compose.yml)
PINNED = {
    "n8n":         "2.20.0",
    "Open WebUI":  "v0.9.2",
    "Qdrant":      "v1.17.1",
    "Caddy":       "2.11.2",
    "llama.cpp":   "server-cuda",
    "LiteLLM":     "latest",
    "ComfyUI":     "v0.20.1",
    "oauth2-proxy":"latest-alpine",
}


def run_cmd(cmd, timeout=30):
    """Run a command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1


def read_hermes_pin():
    """Read HERMES_PINNED_SHA from hermes/Dockerfile (None if missing/malformed)."""
    if not HERMES_DOCKERFILE.exists():
        return None
    text = HERMES_DOCKERFILE.read_text()
    m = re.search(r"^ARG HERMES_PINNED_SHA=([a-f0-9]+)", text, re.MULTILINE)
    return m.group(1) if m else None


def fetch_tag_sha(repo, tag):
    """Resolve a tag name to its commit SHA via the GitHub API.

    Handles both lightweight tags (object points directly at the commit) and
    annotated tags (object points at a tag object, which must be dereferenced).
    """
    cmd = ["curl", "-s", "--max-time", "15", "-L",
           "-H", "Accept: application/vnd.github.v3+json",
           "-H", "User-Agent: Ordo-AI-Stack-Monitor/3.0",
           f"https://api.github.com/repos/{repo}/git/refs/tags/{tag}"]
    stdout, _, rc = run_cmd(cmd)
    if rc != 0 or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
        obj = data.get("object", {})
        sha = obj.get("sha")
        if obj.get("type") == "tag" and sha:
            # Annotated tag — dereference to the commit it points at.
            cmd2 = ["curl", "-s", "--max-time", "15", "-L",
                    "-H", "Accept: application/vnd.github.v3+json",
                    "-H", "User-Agent: Ordo-AI-Stack-Monitor/3.0",
                    f"https://api.github.com/repos/{repo}/git/tags/{sha}"]
            stdout2, _, rc2 = run_cmd(cmd2)
            if rc2 == 0 and stdout2.strip():
                try:
                    return json.loads(stdout2).get("object", {}).get("sha")
                except json.JSONDecodeError:
                    return None
        return sha
    except json.JSONDecodeError:
        return None


def fetch_compare_ahead(repo, base_sha, head_sha):
    """How many commits is `head_sha` ahead of `base_sha`? Returns int or None."""
    cmd = ["curl", "-s", "--max-time", "15", "-L",
           "-H", "Accept: application/vnd.github.v3+json",
           "-H", "User-Agent: Ordo-AI-Stack-Monitor/3.0",
           f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}"]
    stdout, _, rc = run_cmd(cmd)
    if rc != 0 or not stdout.strip():
        return None
    try:
        return json.loads(stdout).get("ahead_by")
    except json.JSONDecodeError:
        return None


def evaluate_dockerfile_pinned(repo, latest_tag, body):
    """Severity logic for SHA-pinned services (Hermes). Returns dict matching the entry shape."""
    pinned_sha = read_hermes_pin()
    if not pinned_sha:
        return {"pinned": "?", "status": "unknown",
                "message": "Could not read HERMES_PINNED_SHA from hermes/Dockerfile"}
    if latest_tag is None:
        return {"pinned": pinned_sha[:12], "status": "unknown",
                "message": "Could not fetch latest release"}

    latest_sha = fetch_tag_sha(repo, latest_tag)
    if latest_sha is None:
        return {"pinned": pinned_sha[:12], "latest": latest_tag, "status": "unknown",
                "message": f"Could not resolve tag {latest_tag} to SHA"}

    # CVE / security mention in release notes always wins.
    body_lower = (body or "").lower()
    has_cve = bool(re.search(r"CVE-\d{4}-\d{4,}", body or ""))
    sec_kw = ["vulnerability", "exploit", "buffer overflow", "auth bypass",
              "privilege escalation", "injection attack", "denial of service",
              "cve-", "security advisory"]
    is_security = has_cve or any(kw in body_lower for kw in sec_kw)

    if pinned_sha == latest_sha:
        severity = "SAFE"
        message = f"On the latest tagged release ({latest_tag})"
    elif is_security:
        severity = "CRITICAL"
        message = f"Security fix in {latest_tag} - update recommended immediately"
    else:
        ahead = fetch_compare_ahead(repo, pinned_sha, latest_sha)
        severity = "HIGH"  # SHA-pinned with no semver - flag as worth reviewing
        if ahead is not None:
            message = f"{latest_tag} available - {ahead} commits ahead of pinned"
        else:
            message = f"{latest_tag} available - pinned is older"

    return {
        "pinned": f"{pinned_sha[:12]} (Dockerfile)",
        "latest": f"{latest_tag} ({latest_sha[:12]})",
        "severity": severity,
        "message": message,
        "manual_update": True,  # apply_updates can't bump Dockerfiles; user must do this by hand
    }


def fetch_latest_release(repo):
    """Fetch latest release from GitHub API or Atom feed."""
    # Try GitHub API first
    cmd = ["curl", "-s", "--max-time", "20", "-L",
           "-H", "Accept: application/vnd.github.v3+json",
           "-H", "User-Agent: Ordo-AI-Stack-Monitor/3.0",
           f"https://api.github.com/repos/{repo}/releases/latest"]
    stdout, stderr, rc = run_cmd(cmd)
    if rc == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
            if "tag_name" in data:
                return data["tag_name"], data.get("body", ""), data.get("html_url", "")
        except json.JSONDecodeError:
            pass

    # Fall back to Atom feed
    cmd = ["curl", "-s", "--max-time", "20", "-L",
           "-H", "User-Agent: Ordo-AI-Stack-Monitor/3.0",
           f"https://github.com/{repo}/releases.atom?per_page=1"]
    stdout, stderr, rc = run_cmd(cmd)
    if rc == 0 and stdout.strip():
        tag_m = re.search(r'<id>.*?tag:github\.com, [\d-]+.*?v?([\d.]+).*?</id>', stdout)
        url_m = re.search(r'<link[^>]*href="([^"]+)"', stdout)
        body_m = re.search(r'<summary[^>]*>(.*?)</summary>', stdout, re.DOTALL)

        tag = tag_m.group(1) if tag_m else None
        url = url_m.group(1) if url_m else ""
        body = re.sub(r'<[^>]+>', '', body_m.group(1)).strip() if body_m else ""

        if tag:
            return tag, body, url

    return None, "", ""


def classify_severity(current, latest, body=""):
    """Classify update severity: CRITICAL, HIGH, MEDIUM, LOW, SAFE."""
    if latest is None or not body:
        return "LOW", "Unknown update — check manually"

    # Security check — only CRITICAL for actual CVE/vulnerability mentions
    body_lower = body.lower()
    has_cve = bool(re.search(r'CVE-\d{4}-\d{4,}', body))
    real_security_kw = ['vulnerability', 'exploit', 'buffer overflow',
                        'auth bypass', 'privilege escalation', 'injection attack',
                        'denial of service', 'cve-', 'vulnerability in',
                        'security advisory']
    if has_cve or any(kw in body_lower for kw in real_security_kw):
        return "CRITICAL", "Security fix — update recommended immediately"

    # Parse versions — strip v/@ prefixes
    # Handle special cases: n8n@X.Y.Z, etc.
    clean_current = current
    clean_latest = latest
    if clean_current.startswith('n8n@'):
        clean_current = clean_current[4:]
    if clean_latest.startswith('n8n@'):
        clean_latest = clean_latest[4:]
    clean_current = re.sub(r'^[v@]', '', clean_current).strip()
    clean_latest = re.sub(r'^[v@]', '', clean_latest).strip()

    try:
        p_parts = [int(x) for x in re.findall(r'\d+', clean_current)]
        l_parts = [int(x) for x in re.findall(r'\d+', clean_latest)]

        if not p_parts or not l_parts:
            return "MEDIUM", f"Version format unknown ({clean_current} → {clean_latest})"

        max_len = max(len(p_parts), len(l_parts))
        p_parts.extend([0] * (max_len - len(p_parts)))
        l_parts.extend([0] * (max_len - len(l_parts)))

        if l_parts == p_parts:
            return "SAFE", "Already up to date"

        major_diff = l_parts[0] - p_parts[0]
        minor_diff = l_parts[1] - p_parts[1] if len(l_parts) > 1 and len(p_parts) > 1 else 0

        if major_diff > 0:
            return "HIGH", f"Major version jump ({clean_current} → {clean_latest}) — review breaking changes"
        elif minor_diff > 0:
            return "MEDIUM", f"Minor update ({clean_current} → {clean_latest})"
        else:
            return "LOW", f"Patch update ({clean_current} → {clean_latest})"

    except (ValueError, IndexError):
        return "LOW", "Update available"


def extract_highlights(body, max_items=4):
    """Extract key highlights from release body."""
    if not body:
        return []
    lines = []
    for line in body.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('>') or stripped.startswith('<!--'):
            continue
        # Skip markdown headings and section headers
        if re.match(r'^#+\s', stripped):
            continue
        # Strip markdown links and bold/italic for cleaner output
        clean = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', stripped)
        clean = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', clean)
        clean = clean.strip()
        if clean and len(clean) > 10 and not re.match(r'^https?://', clean):
            lines.append(clean[:120])
        if len(lines) >= max_items:
            break
    return lines


def read_compose_versions():
    """Read current pinned versions from docker-compose.yml."""
    text = COMPOSE.read_text()
    versions = {}
    patterns = {
        "n8n": r'docker\.n8n\.io/n8nio/n8n:([\d.]+)',
        "Open WebUI": r'open-webui/open-webui:v([\d.]+)',
        "Qdrant": r'qdrant/qdrant:v([\d.]+)',
        "Caddy": r'caddy:([\d.]+)-alpine',
        "llama.cpp": r'ghcr\.io/ggml-org/llama\.cpp:([a-z-]+)',
        "oauth2-proxy": r'oauth2-proxy/oauth2-proxy:([\w-]+)',
    }
    for name, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            versions[name] = m.group(1)
    return versions


def apply_updates(updates):
    """Apply version updates to docker-compose.yml and github_monitor.py."""
    compose_text = COMPOSE.read_text()
    monitor_text = MONITOR.read_text()
    applied = {}

    for name, new_tag in updates.items():
        # Update docker-compose.yml
        patterns = {
            "n8n": (r'docker\.n8n\.io/n8nio/n8n:[\d.]+', f'docker.n8n.io/n8nio/n8n:{new_tag}'),
            "Open WebUI": (r'open-webui/open-webui:v[\d.]+', f'open-webui/open-webui:v{new_tag}'),
            "Qdrant": (r'qdrant/qdrant:v[\d.]+', f'qdrant/qdrant:v{new_tag}'),
            "Caddy": (r'caddy:([\d.]+)-alpine', f'caddy:{new_tag}-alpine'),
        }
        if name in patterns:
            old_pattern, new_val = patterns[name]
            if re.search(old_pattern, compose_text):
                compose_text = re.sub(old_pattern, new_val, compose_text)
                applied[name] = "docker-compose.yml"

        # Update github_monitor.py PINNED dict
        for key_display in ["n8n", "Open WebUI", "Qdrant", "Caddy"]:
            if key_display.lower() == name.lower():
                key_map = {"n8n": '"n8n"', "Open WebUI": '"Open WebUI"',
                          "Qdrant": '"Qdrant"', "Caddy": '"Caddy"'}
                if key_display in key_map:
                    monitor_text = re.sub(
                        rf'({key_map[key_display]}.*?"pinned":\s*")[\d.v-]+(")',
                        rf'\g<1>{new_tag}\g<2>',
                        monitor_text
                    )
                    if name not in applied:
                        applied[name] = "github_monitor.py"

    # Write updated files
    COMPOSE.write_text(compose_text)
    MONITOR.write_text(monitor_text)

    # Also update the Docker-Only table in github_monitor.py
    if "n8n" in updates:
        monitor_text = MONITOR.read_text()
        monitor_text = re.sub(
            r'(docker\.n8n\.io/n8nio/n8n:[\d.]+)',
            f'docker.n8n.io/n8nio/n8n:{updates["n8n"]}',
            monitor_text
        )
        MONITOR.write_text(monitor_text)

    return applied


def restart_services(services_to_restart):
    """Restart affected Docker services."""
    if not services_to_restart:
        return {}

    results = {}
    for svc in services_to_restart:
        cmd = ["docker", "compose", "up", "-d", "--force-recreate", "--no-build", svc]
        stdout, stderr, rc = run_cmd(cmd, timeout=120)
        results[svc] = "success" if rc == 0 else f"failed: {stderr[:200]}"
    return results


def create_git_branch_and_pr(changes):
    """Create a git branch, commit, push, and create a PR."""
    branch_name = f"update/{datetime.now(UTC).strftime('%Y-%m-%d')}/stack-versions"
    services = list(changes.keys())
    commit_msg = f"chore: update stack versions ({', '.join(services)})"

    # Get current branch
    current_branch, _, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = current_branch.strip()

    # Create and checkout new branch
    run_cmd(["git", "checkout", "-b", branch_name])

    # Add changes
    run_cmd(["git", "add", str(COMPOSE), str(MONITOR)])

    # Commit
    run_cmd(["git", "config", "user.email", "hermes@ordo-ai-stack.local"])
    run_cmd(["git", "config", "user.name", "Hermes Bot"])
    run_cmd(["git", "commit", "-m", commit_msg])

    # Push
    stdout, stderr, rc = run_cmd(["git", "push", "origin", branch_name])
    if rc != 0:
        return {"error": f"Push failed: {stderr[:200]}"}

    # Create PR via GitHub API
    pr_body = f"""## Automated Stack Update

**Date:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}
**Services updated:** {', '.join(services)}

### Changes
"""
    for svc, file in changes.items():
        pr_body += f"- **{svc}**: updated in `{file}`\n"

    pr_body += "\n---\n*Auto-generated by Ordo-AI-Stack Monitor*"

    cmd = ["curl", "-s", "-X", "POST",
           "-H", f"Authorization: token {os.environ.get('GITHUB_TOKEN', '')}",
           "-H", "Accept: application/vnd.github.v3+json",
           "https://api.github.com/repos/AlpineWalker1995/ordo-ai-stack/pulls",
           "-d", json.dumps({
               "title": f"Update stack versions ({', '.join(services)})",
               "body": pr_body,
               "head": branch_name,
               "base": current_branch.strip(),
           })]
    stdout, stderr, rc = run_cmd(cmd)

    return {
        "branch": branch_name,
        "pr_created": rc == 0,
        "pr_url": json.loads(stdout).get("html_url", "") if rc == 0 else None,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ordo-AI-Stack Package Audit")
    parser.add_argument("--apply", action="store_true", help="Apply updates if available")
    parser.add_argument("--approve-file", type=str, default="/tmp/stack_approve.json",
                        help="Path to approved updates JSON")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    compose_versions = read_compose_versions()
    results = {"timestamp": datetime.now(UTC).isoformat(), "services": {}}
    all_updates = {}

    for name, info in SERVICES.items():
        latest_tag, body, url = fetch_latest_release(info["repo"])

        # Branch on pin_source — Dockerfile-pinned services use SHA comparison.
        if info.get("pin_source") == "dockerfile":
            entry = evaluate_dockerfile_pinned(info["repo"], latest_tag, body)
            entry["url"] = url
            entry["highlights"] = extract_highlights(body, max_items=4)
            results["services"][name] = entry
            if entry.get("severity") not in (None, "SAFE"):
                all_updates[name] = latest_tag
            continue

        # Compose-pinned services (the original path).
        current = compose_versions.get(name, PINNED.get(name, "unknown"))

        if latest_tag is None:
            results["services"][name] = {
                "pinned": current, "status": "unknown", "message": "Could not fetch release"
            }
            continue

        severity, message = classify_severity(current, latest_tag, body)
        highlights = extract_highlights(body, max_items=4)

        entry = {
            "pinned": current,
            "latest": latest_tag,
            "severity": severity,
            "message": message,
            "url": url,
            "highlights": highlights,
        }
        results["services"][name] = entry

        if severity != "SAFE":
            all_updates[name] = latest_tag

    results["all_updates"] = all_updates
    results["has_updates"] = len(all_updates) > 0

    # Apply if requested and approved
    if args.apply and all_updates:
        approved_file = Path(args.approve_file)
        approved = {}
        if approved_file.exists():
            try:
                approved = json.loads(approved_file.read_text())
            except (OSError, json.JSONDecodeError):
                pass

        if approved:
            print(f"\nApplying approved updates: {approved}")
            applied = apply_updates(approved)
            results["applied"] = applied

            # Determine services to restart
            restart = [n for n in approved if n in {"n8n", "Open WebUI", "Qdrant", "Caddy"}]
            if restart:
                results["restart"] = restart_services(restart)

            # Create PR
            results["pr"] = create_git_branch_and_pr(applied)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        # Human-readable output
        print("# 📡 Ordo-AI-Stack — Package Audit")
        print(f"**{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}**\n")

        critical = []
        high = []
        medium = []
        low = []
        safe = []

        for name, info in results["services"].items():
            sev = info.get("severity", "LOW")
            entry = f"**{name}**: pinned `{info['pinned']}` → latest `{info.get('latest', '?')}` — {info['message']}"
            if info.get("highlights"):
                for h in info["highlights"]:
                    entry += f"\n  • {h}"
            if info.get("url"):
                entry += f"\n  → {info['url']}"
            entry += "\n"

            if sev == "CRITICAL":
                critical.append(entry)
            elif sev == "HIGH":
                high.append(entry)
            elif sev == "MEDIUM":
                medium.append(entry)
            elif sev == "LOW":
                low.append(entry)
            else:
                safe.append(entry)

        if critical:
            print("## 🔴 CRITICAL (Security)\n")
            for c in critical:
                print(c)
        if high:
            print("## 🟠 HIGH (Major version jump)\n")
            for h in high:
                print(h)
        if medium:
            print("## 🟡 MEDIUM (Minor update)\n")
            for m in medium:
                print(m)
        if low:
            print("## 🟢 LOW (Patch update)\n")
            for entry in low:
                print(entry)
        if safe:
            print("## ✅ SAFE (Up to date)\n")
            for s in safe:
                print(s)

        if all_updates:
            print(f"\n---\n\n**📌 Updates available:** {len(all_updates)} services")
            print("**Recommendation:** Review severity above, then approve updates.")
        else:
            print("\n\n**✅ Everything is up to date.**")

    return 0


if __name__ == "__main__":
    sys.exit(main())
