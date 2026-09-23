"""Contract tests for the dashboard's rag_status(), which /api/overview reads in-process."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def dashboard_with_qdrant():
    import dashboard.app as dashboard_app

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "result": {"points_count": 42, "status": "green"},
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(dashboard_app, "_get_http_client", return_value=mock_client):
        yield dashboard_app


def test_rag_status_returns_ok_and_counts(dashboard_with_qdrant):
    """rag_status() returns ok, collection, points_count when Qdrant responds."""
    data = asyncio.run(dashboard_with_qdrant.rag_status())
    assert data.get("ok") is True
    assert data.get("collection") == "documents"
    assert data.get("points_count") == 42
    assert data.get("status") == "green"


def test_rag_status_empty_collection_404():
    """404 from Qdrant means collection missing — dashboard reports empty collection."""
    import dashboard.app as dashboard_app

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(dashboard_app, "_get_http_client", return_value=mock_client):
        data = asyncio.run(dashboard_app.rag_status())
    assert data.get("ok") is True
    assert data.get("points_count") == 0
    assert data.get("status") == "empty"
