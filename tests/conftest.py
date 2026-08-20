"""Top-level conftest for the ``tests/`` suite.

Several tests import ``services/ops-api/main.py`` via ``spec_from_file_location``
and trigger its module-level ``_audit_log = AuditLog(AUDIT_LOG_PATH)``. The
default path is ``/data/audit.jsonl`` (the production volume mount), and
``AuditLog.__init__`` calls ``mkdir(parents=True)`` on the parent — which
fails with ``PermissionError`` on a clean CI runner where ``/data`` doesn't
exist and isn't writable.

Set a writable default before any test module runs so the import succeeds.
Individual tests that need to inspect the audit file still override
``AUDIT_LOG_PATH`` via ``monkeypatch.setenv`` or by patching
``_audit_log`` directly.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "AUDIT_LOG_PATH",
    str(Path(tempfile.gettempdir()) / "ordo-test-audit.jsonl"),
)

# ``dashboard/app.py`` resolves DASHBOARD_DATA_PATH (default ``./data/dashboard``)
# at import time and loads/saves the throughput store there. Without an override, a
# local pytest run reads AND WRITES the production data dir — the live store was
# found carrying test_throughput_record_accepts_sample's literal payload
# ("test-model" @ 25.5 tok/s). Point it at a per-run temp dir before any test
# imports dashboard.app.
os.environ.setdefault(
    "DASHBOARD_DATA_PATH",
    tempfile.mkdtemp(prefix="ordo-test-dashboard-"),
)
