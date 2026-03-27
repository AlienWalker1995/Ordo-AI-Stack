"""Minimal ComfyUI HTTP client for /prompt + history (orchestration layer)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx


async def queue_prompt(base_url: str, workflow: dict[str, Any], *, client_id: str | None = None) -> str:
    """POST /prompt; returns prompt_id."""
    cid = client_id or str(uuid.uuid4())
    url = f"{base_url.rstrip('/')}/prompt"
    body = {"prompt": workflow, "client_id": cid}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    pid = data.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI /prompt missing prompt_id: {data!r}")
    return str(pid)


async def fetch_history(prompt_id: str, base_url: str) -> dict[str, Any] | None:
    url = f"{base_url.rstrip('/')}/history/{prompt_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def wait_for_outputs(
    prompt_id: str,
    base_url: str,
    *,
    max_wait_sec: float = 600.0,
    poll_interval_sec: float = 1.0,
) -> dict[str, Any]:
    """Poll /history/{prompt_id} until outputs appear or timeout."""
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        hist = await fetch_history(prompt_id, base_url)
        if hist and prompt_id in hist:
            entry = hist[prompt_id]
            if entry.get("outputs"):
                return entry
        await asyncio.sleep(poll_interval_sec)
    raise TimeoutError(f"ComfyUI history for {prompt_id} did not produce outputs within {max_wait_sec}s")
