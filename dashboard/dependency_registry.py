"""Load dependency_registry.json and probe each entry (M7)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

_REGISTRY_PATH = Path(__file__).resolve().parent / "dependency_registry.json"


def load_registry() -> dict[str, Any]:
    with open(_REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _probe_one(url: str, timeout_sec: float = 3.0) -> tuple[bool, float | None, str | None]:
    t0 = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            r = client.get(url)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ok = 200 <= r.status_code < 300
        err = None if ok else f"HTTP {r.status_code}"
        return ok, latency_ms, err
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return False, latency_ms, str(e)


def probe_all() -> dict[str, Any]:
    data = load_registry()
    entries = data.get("entries", [])
    results: list[dict[str, Any]] = []
    for e in entries:
        url = e.get("check_url", "")
        ok, lat, err = _probe_one(url) if url else (False, None, "no check_url")
        row = {
            **e,
            "ok": ok,
            "latency_ms": round(lat, 2) if lat is not None else None,
            "error": err,
        }
        ready_url = e.get("ready_url")
        if ready_url:
            rok, rlat, rerr = _probe_one(ready_url)
            row["ready_ok"] = rok
            row["ready_latency_ms"] = round(rlat, 2) if rlat is not None else None
            row["ready_error"] = rerr
        results.append(row)
    return {
        "version": data.get("version", 1),
        "description": data.get("description", ""),
        "entries": results,
    }
