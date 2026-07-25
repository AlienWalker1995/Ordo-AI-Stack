"""Unit tests for the dashboard dependency HTTP probe (M7).

The probe now lives in dashboard.services_catalog (the single catalog); the old
dependency_registry module/JSON was consolidated away.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_client(status_code: int) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    return client


def test_soft_4xx_counts_as_reachable():
    """Naive GET /mcp returns 400; a soft_4xx endpoint is still up for MCP clients."""
    from dashboard.services_catalog import _probe_one

    client = _make_mock_client(400)
    ok, _lat, err = asyncio.run(
        _probe_one("http://mcp-gateway:8811/mcp", client, soft_4xx=True)
    )
    assert ok is True
    assert err is None


def test_other_services_http_400_still_fails():
    from dashboard.services_catalog import _probe_one

    client = _make_mock_client(400)
    ok, _lat, err = asyncio.run(
        _probe_one("http://model-gateway:11435/health", client, soft_4xx=False)
    )
    assert ok is False
    assert err == "HTTP 400"
