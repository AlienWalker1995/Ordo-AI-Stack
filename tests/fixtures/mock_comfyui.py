"""Minimal ComfyUI mock server for E2E tests. No GPU required."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

_app = FastAPI()
_prompts: dict[str, dict[str, Any]] = {}


@_app.post("/prompt")
async def queue_prompt(body: dict = None):
    pid = str(uuid.uuid4())
    _prompts[pid] = {"status": "pending", "outputs": {}}
    # Simulate async completion after a short delay
    def _complete():
        time.sleep(0.3)
        _prompts[pid] = {
            "status": "completed",
            "outputs": {
                "15": {
                    "gifs": [{"filename": f"social-bot_{pid[:8]}.mp4", "type": "output"}]
                }
            },
        }
    threading.Thread(target=_complete, daemon=True).start()
    return {"prompt_id": pid}


@_app.get("/history/{prompt_id}")
async def get_history(prompt_id: str):
    entry = _prompts.get(prompt_id, {})
    if not entry:
        return JSONResponse({})
    if entry.get("outputs"):
        return JSONResponse({prompt_id: {"outputs": entry["outputs"], "status": {"status_str": "success"}}})
    return JSONResponse({prompt_id: {}})


@_app.get("/system_stats")
async def system_stats():
    return {"system": {"os": "Linux"}, "devices": []}


def start_mock_comfyui(host: str = "127.0.0.1", port: int = 18188) -> threading.Thread:
    """Start mock ComfyUI in a background daemon thread. Returns the thread."""
    config = uvicorn.Config(_app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Wait for server to be ready
    import httpx
    for _ in range(30):
        try:
            httpx.get(f"http://{host}:{port}/system_stats", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return t
