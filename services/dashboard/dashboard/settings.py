"""Environment-derived settings for the dashboard (single source of truth).

Secrets are read with secret_env.read_secret: from the file `<NAME>_FILE` points at (the rendered
delivery, /run/secrets/*), else from `<NAME>`.
"""
from __future__ import annotations

from dashboard.secret_env import read_secret

# The ops-controller bearer. The dashboard sends it to ops-controller, and accepts it from internal
# callers (mcp-orchestration) on its own protected routes (see dashboard/auth.py).
OPS_CONTROLLER_TOKEN: str = read_secret("OPS_CONTROLLER_TOKEN")
# The local operator's sign-in secret. The render passes it only while the edge is off and the
# dashboard is published on loopback (services/dashboard/dashboard.yaml `local_login_secret`), so
# empty means edge mode: there is no local sign-in and SSO through Caddy is the operator's way in.
DASHBOARD_LOCAL_LOGIN_TOKEN: str = read_secret("DASHBOARD_LOCAL_LOGIN_TOKEN")
