"""Load dependency_registry.json and probe each entry (M7)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx as _httpx

_REGISTRY_PATH = Path(__file__).resolve().parent / "dependency_registry.json"


def load_registry() -> dict[str, Any]:
    with open(_REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


async def _probe_one(
    url: str,
    client: _httpx.AsyncClient,
    timeout_sec: float = 3.0,
    *,
    entry_id: str | None = None,
) -> tuple[bool, float | None, str | None]:
    t0 = time.perf_counter()
    try:
        r = await client.get(url, timeout=timeout_sec)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ok = 200 <= r.status_code < 300
        # MCP Streamable HTTP: a bare GET to /mcp is invalid; server often returns 400
        # (e.g. missing Mcp-Session-Id). Clients still reach it via POST/SSE — treat as up.
        if (
            not ok
            and entry_id == "mcp-gateway"
            and r.status_code < 500
        ):
            ok = True
        err = None if ok else f"HTTP {r.status_code}"
        return ok, latency_ms, err
    except (_httpx.RequestError, OSError) as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return False, latency_ms, str(e)


async def _probe_entry(e: dict[str, Any], client: _httpx.AsyncClient) -> dict[str, Any]:
    url = e.get("check_url", "")
    eid = e.get("id")
    ok, lat, err = (
        await _probe_one(url, client, entry_id=eid)
        if url
        else (False, None, "no check_url")
    )
    row = {
        **e,
        "ok": ok,
        "latency_ms": round(lat, 2) if lat is not None else None,
        "error": err,
    }
    ready_url = e.get("ready_url")
    if ready_url:
        rok, rlat, rerr = await _probe_one(ready_url, client, entry_id=eid)
        row["ready_ok"] = rok
        row["ready_latency_ms"] = round(rlat, 2) if rlat is not None else None
        row["ready_error"] = rerr
    return row


async def probe_all(client: _httpx.AsyncClient | None = None) -> dict[str, Any]:
    data = load_registry()
    entries = data.get("entries", [])
    c = client or _httpx.AsyncClient(timeout=3.0, follow_redirects=True)
    try:
        results = await asyncio.gather(*[_probe_entry(e, c) for e in entries])
    finally:
        if client is None:
            await c.aclose()
    return {
        "version": data.get("version", 1),
        "description": data.get("description", ""),
        "entries": list(results),
    }
