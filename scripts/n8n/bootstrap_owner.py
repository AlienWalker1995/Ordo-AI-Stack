#!/usr/bin/env python3
"""Apply N8N_OWNER_EMAIL and N8N_OWNER_PASSWORD to the running n8n instance.

n8n 2.x's authenticationMethod is constrained to [email, ldap, saml], so any
reverse-proxy SSO sits in front of an internal email/password login form.
This script makes those credentials manageable from a single place (.env)
instead of buried in a setup wizard or one-off shell commands.

Idempotent. Safe to re-run any time:

  - First run on an empty install: uses n8n's /rest/owner/setup endpoint to
    bootstrap the owner cleanly.
  - Re-run with same credentials: detects no change, exits.
  - Re-run with different email or password: updates the existing global:owner
    row's email + bcrypt password hash directly via the SQLite database.
    Existing user_api_keys rows (which reference the owner by UUID, not by
    credentials) survive intact.

Run from inside the docker network so n8n is reachable. Suggested invocation
documented in .env.example.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

# bcrypt is needed for the DB-update path; in pip-managed runs the operator
# installs it inline. If unavailable we'll only support the fresh-install path
# (via /rest/owner/setup which hashes server-side).
try:
    import bcrypt  # type: ignore
except ImportError:
    bcrypt = None  # type: ignore[assignment]

N8N_BASE = os.environ.get("N8N_BASE_URL", "http://n8n:5678")
DB_PATH = Path(os.environ.get(
    "N8N_DB_PATH",
    "/repo/data/n8n-data/database.sqlite",
))


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    url = f"{N8N_BASE.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except ValueError:
            return e.code, body_text


def _db_owner_row() -> tuple[str, str | None, str | None] | None:
    """Return (id, email, password_hash) for the global:owner user, or None."""
    if not DB_PATH.is_file():
        return None
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "SELECT id, email, password FROM user WHERE roleSlug='global:owner' LIMIT 1"
        )
        row = cur.fetchone()
        return tuple(row) if row else None
    finally:
        con.close()


def _db_update_owner(user_id: str, email: str, password: str) -> None:
    if bcrypt is None:
        raise RuntimeError(
            "bcrypt module required for DB-update path. Install with "
            "`pip install bcrypt` and re-run, or wipe data/n8n-data/ to use "
            "the fresh-install /rest/owner/setup path instead."
        )
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10))
    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute(
            "UPDATE user SET email=?, password=? WHERE id=?",
            (email, pw_hash.decode("utf-8"), user_id),
        )
        con.execute(
            "UPDATE settings SET value='true' "
            "WHERE key='userManagement.isInstanceOwnerSetUp'"
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    email = os.environ.get("N8N_OWNER_EMAIL", "").strip()
    password = os.environ.get("N8N_OWNER_PASSWORD", "")
    if not email or not password:
        print(
            "ERROR: N8N_OWNER_EMAIL and N8N_OWNER_PASSWORD must both be set.",
            file=sys.stderr,
        )
        print("Edit .env and re-run.", file=sys.stderr)
        return 1

    print(f"==> n8n at {N8N_BASE}, DB at {DB_PATH}")

    existing = _db_owner_row()
    if existing is None:
        # No DB visible from this container, or no owner row yet. Try the
        # public setup endpoint — works on a fresh install only.
        print("==> no owner row visible; trying /rest/owner/setup (fresh install path)")
        status, body = _http("POST", "/rest/owner/setup", {
            "email": email,
            "password": password,
            "firstName": "Owner",
            "lastName": "Operator",
        })
        if status == 200:
            print("  OK — owner bootstrapped via setup endpoint")
            return 0
        if status == 400 and isinstance(body, dict) and "already setup" in str(body).lower():
            print(
                "  /rest/owner/setup refused (owner already set up) and no DB "
                "access from here. Mount data/n8n-data into this container and "
                "re-run, or run on the host.",
                file=sys.stderr,
            )
            return 2
        print(f"  setup failed: HTTP {status} {body!r}", file=sys.stderr)
        return 3

    user_id, current_email, current_pw_hash = existing
    print(f"==> existing owner: id={user_id} email={current_email!r}")

    if current_email == email and current_pw_hash and bcrypt is not None:
        # Password may match. Test before deciding to update.
        if bcrypt.checkpw(password.encode("utf-8"), current_pw_hash.encode("utf-8")):
            print("==> email + password already match — nothing to do")
            return 0

    print("==> updating owner email + password hash in DB")
    _db_update_owner(user_id, email, password)
    print("  done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
