"""Top-level conftest for the ``tests/`` suite.

Constructing the control plane (``ordo/control/api.py``) opens its audit log at
``AUDIT_LOG_PATH``, default ``/data/audit.jsonl`` (the production volume mount),
and creates the parent directory, which fails with ``PermissionError`` on a clean
CI runner where ``/data`` doesn't exist and isn't writable.

Set a writable default before any test module runs so the import succeeds.
Individual tests that need to inspect the audit file still override
``AUDIT_LOG_PATH`` via ``monkeypatch.setenv`` or by patching
``_audit_log`` directly.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

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


def _rendered_services_catalog() -> str:
    """Write the service catalog `ordo render` would write, and return its path.

    The dashboard runs against the RENDERED catalog (out/services-catalog.json, mounted at
    SERVICES_CATALOG_PATH), which carries fields the render derives rather than the fragments
    declare: a card's `sso_port` comes from its owner's `edge_site`. Point the dashboard's
    catalog loader at the same document before any test imports it, so the tests exercise what
    production serves. tests/test_services_catalog_fragments.py checks the fragment fallback."""
    from ordo.render.engine import aggregate_services_catalog

    path = Path(tempfile.mkdtemp(prefix="ordo-test-catalog-")) / "services-catalog.json"
    path.write_text(json.dumps(aggregate_services_catalog(), indent=2) + "\n", encoding="utf-8")
    return str(path)


os.environ.setdefault("SERVICES_CATALOG_PATH", _rendered_services_catalog())


# The dashboard refuses anonymous callers on its state-changing and ops-forwarding routes
# (services/dashboard/dashboard/auth.py). Route-behaviour tests authenticate the way an internal
# caller does: with the ops-controller bearer, configured for the test.
DASHBOARD_TEST_OPS_TOKEN = "test-ops-controller-token"


@pytest.fixture
def dashboard_operator_headers(monkeypatch) -> dict[str, str]:
    import dashboard.settings as dashboard_settings

    monkeypatch.setattr(dashboard_settings, "OPS_CONTROLLER_TOKEN", DASHBOARD_TEST_OPS_TOKEN)
    return {"Authorization": f"Bearer {DASHBOARD_TEST_OPS_TOKEN}"}
