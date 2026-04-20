"""Environment-derived settings for the dashboard (single source of truth)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DASHBOARD_AUTH_TOKEN: str = os.environ.get("DASHBOARD_AUTH_TOKEN", "").strip()
AUTH_REQUIRED: bool = bool(DASHBOARD_AUTH_TOKEN)
