"""Test ops-controller audit event schema."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Mock docker before loading ops-controller (avoids requiring docker package)
sys.modules["docker"] = MagicMock()

# Load ops-controller/main.py (folder has hyphen, not valid module name)
_ops_controller_path = Path(__file__).resolve().parent.parent / "ops-controller" / "main.py"
_spec = importlib.util.spec_from_file_location("ops_controller_main", _ops_controller_path)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


def test_audit_schema_fields():
    """_audit writes entries with ts, action, resource, actor, result, detail."""

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.log"
        oc.AUDIT_LOG_PATH = audit_path

        oc._audit("restart", "llamacpp", "ok", "")

        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "ts" in entry
        assert entry["action"] == "restart"
        assert entry["resource"] == "llamacpp"
        assert entry["actor"] == "dashboard"
        assert entry["result"] == "ok"
        assert entry["detail"] == ""


def test_audit_schema_error_result():
    """_audit writes result=error and detail when provided."""

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.log"
        oc.AUDIT_LOG_PATH = audit_path

        oc._audit("restart", "llamacpp", "error", "container not found")

        lines = audit_path.read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert entry["result"] == "error"
        assert entry["detail"] == "container not found"


def test_audit_schema_correlation_id():
    """_audit includes correlation_id when provided."""

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.log"
        oc.AUDIT_LOG_PATH = audit_path

        oc._audit("logs", "dashboard", "ok", "", correlation_id="req-abc123")

        lines = audit_path.read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert entry["correlation_id"] == "req-abc123"


def test_audit_log_rotates_when_over_max_bytes():
    """_maybe_rotate_audit_log renames audit.log to audit.log.1 when over AUDIT_LOG_MAX_BYTES."""

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.log"
        audit_path.write_text("x" * 500)
        oc.AUDIT_LOG_PATH = audit_path
        oc.AUDIT_LOG_MAX_BYTES = 100
        oc._maybe_rotate_audit_log()
        assert not audit_path.exists()
        rotated = Path(tmp) / "audit.log.1"
        assert rotated.exists()
        assert rotated.stat().st_size == 500


def test_audit_after_rotation_writes_fresh_file():
    """After rotation, _audit appends to a new audit.log."""

    with tempfile.TemporaryDirectory() as tmp:
        audit_path = Path(tmp) / "audit.log"
        audit_path.write_text("x" * 200)
        oc.AUDIT_LOG_PATH = audit_path
        oc.AUDIT_LOG_MAX_BYTES = 100
        oc._maybe_rotate_audit_log()
        oc._audit("ping", "test", "ok", "")
        assert audit_path.exists()
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "ping"
