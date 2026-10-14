"""The edge probe behind a card's `edge_check` (services/dashboard/dashboard/services_catalog.py).

It must test the route users take: Caddy's TLS listener on the card's edge port, the tailnet host as
the TLS name (so the certificate verifies), the UI's public host in `Host`, and read red on a 5xx
from the edge, which a container-level check cannot see.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "dashboard"))

from dashboard.services_catalog import _check_via_edge  # noqa: E402

CARD = {"id": "hermes", "sso_port": 8447, "tailnet_label": "hermes", "edge_check": "/api/status"}


def probe(handler, monkeypatch, card=CARD, env=True):
    if env:
        monkeypatch.setenv("CADDY_TAILNET_HOSTNAME", "box.example.ts.net")
        monkeypatch.setenv("CADDY_TAILNET_DOMAIN", "example.ts.net")
    else:
        monkeypatch.delenv("CADDY_TAILNET_HOSTNAME", raising=False)
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            return await _check_via_edge(card, client)

    return asyncio.run(run()), seen


def test_probes_the_edge_port_with_the_public_host_and_the_tailnet_tls_name(monkeypatch):
    (ok, err), seen = probe(lambda r: httpx.Response(200), monkeypatch)
    assert (ok, err) == (True, "")
    request = seen[0]
    assert str(request.url) == "https://caddy:8447/api/status"
    assert request.headers["Host"] == "hermes.example.ts.net"
    assert request.extensions["sni_hostname"] == "box.example.ts.net"


def test_an_edge_error_reads_red(monkeypatch):
    (ok, err), _ = probe(lambda r: httpx.Response(502), monkeypatch)
    assert ok is False and "502" in err


def test_an_unreachable_edge_reads_red(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    (ok, err), _ = probe(refuse, monkeypatch)
    assert ok is False and "refused" in err


def test_no_edge_is_neutral_not_red(monkeypatch):
    (ok, err), seen = probe(lambda r: httpx.Response(200), monkeypatch, env=False)
    assert (ok, err) == (None, "") and not seen
