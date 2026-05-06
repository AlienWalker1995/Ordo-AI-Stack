"""Top-level conftest for the ``tests/`` suite.

Several tests import ``ops-controller/main.py`` via ``spec_from_file_location``
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
