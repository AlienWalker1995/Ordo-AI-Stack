#!/usr/bin/env python3
"""Deploy the social-media fan-out workflow to the local n8n instance.

Idempotent. Reads the workflow JSON from n8n/workflows/social_media_fanout.json,
seeds n8n Variables from ~/.instagram_creds (so the IG branch can resolve
$vars.IG_USER_ID / $vars.IG_ACCESS_TOKEN), and either creates or updates the
workflow by name. Activates it on success and prints the webhook URL.

Auth: reads the n8n API key from $HOME/.ai-toolkit/runtime/secrets/n8n_api_key
(decrypted from secrets/n8n_api_key.sops by scripts/secrets/decrypt.sh).

Talks to n8n over the docker network when run from inside a stack container,
or via http://localhost:5678 when run from the host with the port published.
Override with N8N_API_URL.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / "n8n" / "workflows" / "social_media_fanout.json"
DEFAULT_API_BASE = "http://localhost:5678"
DEFAULT_KEY_PATH = Path.home() / ".ai-toolkit" / "runtime" / "secrets" / "n8n_api_key"
DEFAULT_IG_CREDS = Path.home() / ".instagram_creds"


def _api(api_base: str, key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = f"{api_base.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-N8N-API-KEY": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _load_ig_creds(path: Path) -> dict[str, str]:
    """Parse ~/.instagram_creds. File format: IG_USER_ID=... / ACCESS_TOKEN=... .

    Missing file is not fatal — workflow still deploys, IG branch just fails at
    runtime with a clear error until variables are set.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _set_variable(api_base: str, key: str, name: str, value: str) -> None:
    """Upsert an n8n Variable. n8n's variables API doesn't support PATCH by key,
    so we list, find by name, then POST (create) or PUT (update)."""
    status, items = _api(api_base, key, "GET", "/api/v1/variables")
    if status != 200 or not isinstance(items, dict):
        raise RuntimeError(f"GET /variables failed: {status} {items!r}")
    existing = next(
        (v for v in items.get("data", []) if v.get("key") == name),
        None,
    )
    if existing:
        vid = existing["id"]
        if existing.get("value") == value:
            print(f"  var {name}: unchanged")
            return
        status, body = _api(api_base, key, "PUT", f"/api/v1/variables/{vid}", {"key": name, "value": value})
        if status not in (200, 204):
            raise RuntimeError(f"PUT /variables/{vid} failed: {status} {body!r}")
        print(f"  var {name}: updated")
    else:
        status, body = _api(api_base, key, "POST", "/api/v1/variables", {"key": name, "value": value})
        if status not in (200, 201):
            raise RuntimeError(f"POST /variables failed: {status} {body!r}")
        print(f"  var {name}: created")


def _find_workflow_by_name(api_base: str, key: str, name: str) -> dict | None:
    status, body = _api(api_base, key, "GET", "/api/v1/workflows?limit=100")
    if status != 200:
        raise RuntimeError(f"GET /workflows failed: {status} {body!r}")
    for wf in body.get("data", []):
        if wf.get("name") == name:
            return wf
    return None


def _strip_for_create(wf: dict) -> dict:
    """n8n's create endpoint rejects fields that only make sense on existing
    workflows. Strip them; keep just the canonical create payload."""
    allowed = {"name", "nodes", "connections", "settings", "staticData"}
    return {k: v for k, v in wf.items() if k in allowed}


def main() -> int:
    api_base = os.environ.get("N8N_API_URL", DEFAULT_API_BASE)
    key_path = Path(os.environ.get("N8N_API_KEY_FILE", str(DEFAULT_KEY_PATH)))
    ig_creds_path = Path(os.environ.get("IG_CREDS_FILE", str(DEFAULT_IG_CREDS)))

    if not key_path.is_file():
        print(f"ERROR: n8n API key file not found: {key_path}", file=sys.stderr)
        print("Run scripts/secrets/decrypt.sh first to materialize it.", file=sys.stderr)
        return 1
    key = key_path.read_text(encoding="utf-8").strip()

    if not WORKFLOW_PATH.is_file():
        print(f"ERROR: workflow JSON not found: {WORKFLOW_PATH}", file=sys.stderr)
        return 1
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    wf_name = workflow["name"]

    print(f"==> n8n at {api_base}")

    print("==> seeding variables from ~/.instagram_creds")
    ig = _load_ig_creds(ig_creds_path)
    if ig.get("IG_USER_ID"):
        _set_variable(api_base, key, "IG_USER_ID", ig["IG_USER_ID"])
    else:
        print("  var IG_USER_ID: SKIPPED (not in creds file — IG branch will fail until set)")
    token = ig.get("ACCESS_TOKEN") or ig.get("IG_ACCESS_TOKEN")
    if token:
        _set_variable(api_base, key, "IG_ACCESS_TOKEN", token)
    else:
        print("  var IG_ACCESS_TOKEN: SKIPPED (not in creds file — IG branch will fail until set)")

    print(f"==> upserting workflow '{wf_name}'")
    existing = _find_workflow_by_name(api_base, key, wf_name)
    if existing:
        wf_id = existing["id"]
        status, body = _api(
            api_base, key, "PUT", f"/api/v1/workflows/{wf_id}",
            {
                "name": workflow["name"],
                "nodes": workflow["nodes"],
                "connections": workflow["connections"],
                "settings": workflow.get("settings", {}),
            },
        )
        if status != 200:
            raise SystemExit(f"PUT /workflows/{wf_id} failed: {status} {body!r}")
        print(f"  updated existing workflow id={wf_id}")
    else:
        status, body = _api(api_base, key, "POST", "/api/v1/workflows", _strip_for_create(workflow))
        if status not in (200, 201):
            raise SystemExit(f"POST /workflows failed: {status} {body!r}")
        wf_id = body["id"]
        print(f"  created workflow id={wf_id}")

    print(f"==> activating workflow id={wf_id}")
    status, body = _api(api_base, key, "POST", f"/api/v1/workflows/{wf_id}/activate")
    if status not in (200, 204):
        # Some n8n versions return 400 if already active; not fatal.
        print(f"  activate returned HTTP {status}: {body!r}")
    else:
        print("  active")

    print()
    print("DEPLOY OK")
    print(f"  webhook URL (production, after activation): {api_base.rstrip('/')}/webhook/social-fanout")
    print(f"  workflow id: {wf_id}")
    print()
    print("Try it:")
    print(
        '  curl -X POST http://localhost:5678/webhook/social-fanout '
        '-H "Content-Type: application/json" '
        '-d \'{"caption":"hello world","image_url":"https://example.com/x.jpg","platforms":["instagram","facebook","tiktok"]}\''
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
