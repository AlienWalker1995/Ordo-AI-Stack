"""Environment-derived settings for the dashboard (single source of truth)."""
from __future__ import annotations

import os

# The ops-controller bearer. The dashboard sends it to ops-controller, and accepts it from internal
# callers (mcp-orchestration) on its own protected routes (see dashboard/auth.py).
OPS_CONTROLLER_TOKEN: str = os.environ.get("OPS_CONTROLLER_TOKEN", "").strip()
