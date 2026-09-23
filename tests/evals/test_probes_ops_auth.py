"""The eval runner's ops-controller probe authenticates.

ops-controller refuses every call without `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` (only the
health probe is open), so an unauthenticated /status read would turn every GPU-lease guard into
"ops-controller unreachable".
"""
from __future__ import annotations

import httpx
import respx
from ordo_evals.probes import LiveProbes
from ordo_evals.settings import Settings

OPS = "http://ops-controller:9000"


@respx.mock
def test_ops_status_sends_the_bearer_token(tmp_path):
    route = respx.get(f"{OPS}/status").mock(return_value=httpx.Response(200, json={"gpu": {"state": "idle"}}))
    probes = LiveProbes(vault_dir=tmp_path, ops_controller_url=OPS, ops_controller_token="tok-123",
                        n8n_url="http://n8n:5678", qdrant_url="http://qdrant:6333")
    assert probes.ops_status()["gpu"]["state"] == "idle"
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-123"


def test_settings_read_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "tok-456")
    assert Settings.from_env().ops_controller_token == "tok-456"
