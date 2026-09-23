"""Endpoints behind the Overview, Services, Models, Media and Performance pages.

Each route fetches what it needs concurrently and hands plain dicts to dashboard.console, which
decides what they mean. The fetchers are small module-level functions so tests can replace them
with payloads in the live shapes; nothing here keeps state of its own.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from dashboard import console

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["console"])

GGUF_DIR = Path(os.environ.get("GGUF_MODELS_DIR", "/gguf-models"))
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://comfyui:8188").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://llamacpp:8080").rstrip("/")
LLAMACPP_CPU_URL = os.environ.get("LLAMACPP_CPU_URL", "http://llamacpp-cpu:8080").rstrip("/")
LLAMACPP_EMBED_URL = os.environ.get("LLAMACPP_EMBED_URL", "http://llamacpp-embed:8080").rstrip("/")

# The Grafana dashboard the Performance page embeds (monitoring/grafana/dashboards), kiosk mode.
GRAFANA_EMBED_PATH = "/grafana/d/ordo-llm-gpu/ordo-performance?orgId=1&kiosk&theme=dark&refresh=10s"

# Tokens generated per second by each llama.cpp server, summed per scrape job. A rate over the
# counter rather than the instantaneous gauge, because the gauge goes stale when a server is
# evicted and a sparkline of stale samples draws a flat line that never happened.
_TOKEN_RATE_QUERY = "sum by (job) (rate(llamacpp:tokens_predicted_total[5m]))"

_SEGMENT = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
_RECREATE_TIMEOUT = 660.0


# ---------------------------------------------------------------------------------------------
# fetchers (patched in tests)
# ---------------------------------------------------------------------------------------------

async def _ops_json(path: str) -> dict | None:
    from dashboard.app import _ops_request

    code, data = await _ops_request("GET", path, timeout=15.0)
    return data if code == 200 and isinstance(data, dict) else None


async def _ops_call(method: str, path: str, json: dict | None = None) -> tuple[int, dict]:
    from dashboard.app import _ops_request

    kwargs = {"json": json} if json is not None else {}
    return await _ops_request(method, path, timeout=_RECREATE_TIMEOUT, **kwargs)


async def _get_json(url: str, timeout: float = 10.0) -> dict | None:
    from dashboard.app import _get_http_client

    try:
        r = await _get_http_client().get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception as exc:  # a dependency being down is data, not an error page
        logger.debug("GET %s failed: %s", url, exc)
        return None


async def _comfy_json(path: str) -> dict | None:
    return await _get_json(f"{COMFYUI_URL}{path}", timeout=10.0)


async def _comfy_bytes(filename: str, subfolder: str, kind: str) -> tuple[bytes, str] | None:
    from dashboard.app import _get_http_client

    try:
        r = await _get_http_client().get(
            f"{COMFYUI_URL}/view",
            params={"filename": filename, "subfolder": subfolder, "type": kind},
            timeout=30.0,
        )
    except Exception as exc:
        logger.debug("ComfyUI /view failed: %s", exc)
        return None
    if r.status_code != 200:
        return None
    return r.content, r.headers.get("content-type", "application/octet-stream")


async def _hardware() -> dict:
    from dashboard.app import hardware_stats

    return await hardware_stats()


async def _service_cards() -> list[dict]:
    from dashboard.routes_hub import services

    return (await services()).get("services", [])


async def _rag() -> dict:
    from dashboard.app import rag_status

    return await rag_status()


async def _throughput() -> dict:
    from dashboard.app import throughput_stats

    return await throughput_stats()


async def _disk_files() -> list[dict]:
    from dashboard.app import llm_models

    return (await llm_models()).get("models", [])


async def _served() -> dict:
    """What each llama.cpp server actually loaded, from its own /v1/models."""
    gpu, cpu, embed = await asyncio.gather(
        _get_json(f"{LLAMACPP_URL}/v1/models", 5.0),
        _get_json(f"{LLAMACPP_CPU_URL}/v1/models", 5.0),
        _get_json(f"{LLAMACPP_EMBED_URL}/v1/models", 5.0),
    )
    return {"gpu": console.served_file(gpu), "cpu": console.served_file(cpu),
            "embed": console.served_file(embed)}


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def _containers(services_payload: dict | None) -> dict:
    return {s["id"]: s for s in (services_payload or {}).get("services", []) if s.get("id")}


def _registry(payload: dict | None) -> dict:
    return (payload or {}).get("models", {}) or {}


def _status_line(status_gpu: dict | None, attention: list[dict]) -> dict:
    if status_gpu is None:
        return {"level": "unknown", "text": "The control plane is not answering"}
    if not attention:
        return {"level": "ok", "text": "All systems normal"}
    level = "critical" if any(a["severity"] == "critical" for a in attention) else "warning"
    n = len(attention)
    return {"level": level, "text": f"{n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you"}


# ---------------------------------------------------------------------------------------------
# overview + activity
# ---------------------------------------------------------------------------------------------

@router.get("/overview")
async def overview() -> dict:
    status, services, registry, hardware, cards, rag, throughput, served = await asyncio.gather(
        _ops_json("/status"), _ops_json("/services"), _ops_json("/registry/models"),
        _hardware(), _service_cards(), _rag(), _throughput(), _served(),
    )
    status_gpu = (status or {}).get("gpu") if status else None
    containers = _containers(services)
    attention = console.build_attention(cards, containers, hardware, status_gpu)
    engine = console.chat_engine(status_gpu, containers)
    stats = (throughput or {}).get("models") or {}
    gpu_file = served.get("gpu") or console.resident_gpu_file(_registry(registry))
    verdicts = [console.classify_container(c.get("state"), c.get("health"), c.get("status"))
                for c in containers.values()]
    return {
        "status": _status_line(status_gpu, attention),
        "services": {"up": sum(v == "up" for v in verdicts), "total": len(verdicts)},
        "gpus": console.gpu_cards((hardware or {}).get("gpus") or [], _registry(registry), status_gpu),
        "chat": {**engine,
                 "gpu_p50": (stats.get(gpu_file) or {}).get("p50") if gpu_file else None,
                 "cpu_p50": (stats.get(served.get("cpu")) or {}).get("p50") if served.get("cpu") else None},
        "attention": attention,
        "host": {k: (hardware or {}).get(k) for k in
                 ("cpu_pct", "ram_used_gb", "ram_total_gb", "ram_pct",
                  "disk_used_gb", "disk_total_gb", "disk_pct")},
        "knowledge": {"documents": (rag or {}).get("points_count") if (rag or {}).get("ok") else None},
        "links": [{"name": c["name"], "url": c["open_url"]} for c in cards
                  if c.get("open_url") and not c.get("background")],
        "generated_at": time.time(),
    }


@router.get("/activity")
async def activity(limit: int = Query(25, ge=1, le=100)) -> dict:
    history, audit, renders = await asyncio.gather(
        _ops_json("/jobs/history"), _ops_json("/audit?limit=100"), _comfy_json("/history?max_items=40"),
    )
    items = console.merge_activity(
        (history or {}).get("history", []), (audit or {}).get("entries", []),
        console.summarise_renders(renders or {}, 40), limit,
    )
    return {"items": items}


# ---------------------------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------------------------

@router.get("/services/table")
async def services_table() -> dict:
    cards, services = await asyncio.gather(_service_cards(), _ops_json("/services"))
    return {"groups": console.build_service_table(cards, _containers(services)),
            "control_plane": services is not None}


# ---------------------------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------------------------

@router.get("/models")
async def models() -> dict:
    model_config, served, disk, registry, throughput = await asyncio.gather(
        _ops_json("/model-config"), _served(), _disk_files(), _ops_json("/registry/models"), _throughput(),
    )
    if model_config is None:
        raise HTTPException(status_code=503, detail="The control plane is not answering")
    return console.model_slots(model_config, served, disk, _registry(registry), throughput)


class SwitchBody(BaseModel):
    model: str


# One switch at a time: two interleaving would render one model and recreate for another.
_switch_lock = asyncio.Lock()


@router.post("/models/switch")
async def switch_model(body: SwitchBody) -> dict:
    """Switch the GPU chat model the one safe way: name a catalog entry in the source, let the
    control plane render, then recreate what the plan names. Never an .env edit, which the next
    render would silently undo and which skips the entry's sampler, projector and context."""
    if _switch_lock.locked():
        raise HTTPException(status_code=409, detail="A model switch is already in progress")
    async with _switch_lock:
        return await _switch(body)


async def _switch(body: SwitchBody) -> dict:
    model_config, disk = await asyncio.gather(_ops_json("/model-config"), _disk_files())
    if model_config is None:
        raise HTTPException(status_code=503, detail="The control plane is not answering")
    entry = next((m for m in model_config.get("available", []) if m.get("id") == body.model), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"'{body.model}' is not in the model catalog")
    if entry.get("file") not in {f.get("name") for f in disk}:
        raise HTTPException(status_code=409, detail=f"'{body.model}' is not downloaded: "
                                                    f"{entry.get('file')} is not on disk")
    code, rendered = await _ops_call("POST", "/model-config", {"model": body.model})
    if code != 200:
        raise HTTPException(status_code=502, detail=rendered.get("error") or rendered.get("detail")
                            or f"render failed ({code})")
    plan = console.switch_plan(model_config.get("ctx_size"), rendered.get("ctx_size"))
    recreated = []
    for service in plan["recreate"]:
        # The operator already confirmed the switch in the page; the control plane wants that
        # confirmation carried on every destructive call.
        rc, data = await _ops_call("POST", f"/services/{service}/recreate", {"confirm": True})
        if rc != 200:
            raise HTTPException(status_code=502, detail=f"recreating {service} failed: "
                                                        f"{data.get('error') or data.get('detail') or rc}")
        recreated.append(service)
    return {"ok": True, "active_model": rendered.get("active_model"), "ctx_size": rendered.get("ctx_size"),
            "recreated": recreated, "hermes_restart_needed": plan["hermes_restart_needed"]}


class DeleteBody(BaseModel):
    file: str


@router.post("/models/delete")
async def delete_model(body: DeleteBody) -> dict:
    name = (body.file or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name or not name.lower().endswith(".gguf"):
        raise HTTPException(status_code=400, detail="Name a single .gguf file")
    served, registry = await asyncio.gather(_served(), _ops_json("/registry/models"))
    if name in console.in_use_files(served, _registry(registry)):
        raise HTTPException(status_code=409, detail=f"{name} is in use by a running model server")
    path = GGUF_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{name} is not on disk")
    path.unlink()
    logger.info("MODEL_DELETED file=%s", name)
    return {"ok": True, "deleted": name}


# ---------------------------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------------------------

@router.get("/media")
async def media(limit: int = Query(24, ge=1, le=100)) -> dict:
    queue, history = await asyncio.gather(_comfy_json("/queue"), _comfy_json(f"/history?max_items={limit}"))
    if queue is None:
        raise HTTPException(status_code=503, detail="ComfyUI is not answering")
    return console.summarise_media(queue, history or {}, limit)


@router.get("/media/view")
async def media_view(filename: str = "", subfolder: str = "", type: str = "output") -> Response:  # noqa: A002
    """Proxy one finished output for a thumbnail. Only `output` files, bare names, and safe
    subfolder segments: this reaches into ComfyUI's filesystem on the caller's behalf."""
    if type != "output" or not filename or not _SEGMENT.match(filename) or ".." in filename:
        raise HTTPException(status_code=400, detail="Only a bare output filename can be viewed")
    if subfolder and (".." in subfolder or not all(_SEGMENT.match(s) for s in subfolder.split("/"))):
        raise HTTPException(status_code=400, detail="Invalid subfolder")
    got = await _comfy_bytes(filename, subfolder, type)
    if got is None:
        raise HTTPException(status_code=404, detail="Not found")
    content, content_type = got
    return Response(content=content, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------------------------

@router.get("/perf/series")
async def perf_series(hours: int = Query(24, ge=1, le=168)) -> dict:
    end = int(time.time())
    start = end - hours * 3600
    step = max(60, hours * 3600 // 288)
    query = urlencode({"query": _TOKEN_RATE_QUERY, "start": start, "end": end, "step": step})
    data = await _get_json(f"{PROMETHEUS_URL}/api/v1/query_range?{query}", timeout=15.0)
    if not data or data.get("status") != "success":
        return {"available": False, "gpu": [], "cpu": []}
    series: dict[str, list[list[Any]]] = {"gpu": [], "cpu": []}
    for result in data.get("data", {}).get("result", []):
        key = {"llamacpp": "gpu", "llamacpp-cpu": "cpu"}.get(result.get("metric", {}).get("job"))
        if key:
            series[key] = [[int(t), round(float(v), 2)] for t, v in result.get("values", [])]
    return {"available": True, **series}


@router.get("/perf/grafana")
async def perf_grafana() -> dict:
    health = await _get_json(f"{GRAFANA_URL}/api/health", timeout=5.0)
    return {"available": bool(health and health.get("database") == "ok"), "path": GRAFANA_EMBED_PATH}
