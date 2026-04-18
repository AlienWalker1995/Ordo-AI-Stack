"""Service list for dashboard health/UI and ops ID mapping. Separated from app.py for maintainability."""
from __future__ import annotations

import httpx as _httpx

from dashboard.settings import (
    OPENCLAW_GATEWAY_INTERNAL_PORT as _OPENCLAW_GATEWAY_INTERNAL_PORT,
)
from dashboard.settings import (
    OPENCLAW_GATEWAY_PORT as _OPENCLAW_GATEWAY_PORT,
)
from dashboard.settings import (
    OPENCLAW_GATEWAY_TOKEN as _OPENCLAW_GATEWAY_TOKEN,
)
from dashboard.settings import (
    OPENCLAW_UI_PORT as _OPENCLAW_UI_PORT,
)

# Map dashboard service id -> ops-controller service id
OPS_SERVICE_MAP = {
    "llamacpp": "llamacpp",
    "model-gateway": "model-gateway",
    "webui": "open-webui",
    "mcp": "mcp-gateway",
    "comfyui": "comfyui",
    "n8n": "n8n",
    "openclaw": "openclaw-gateway",
    "qdrant": "qdrant",
}

SERVICES = [
    {"id": "llamacpp", "name": "llama.cpp", "port": 8080, "url": "http://localhost:8080", "check": "http://llamacpp:8080/health", "has_gpu": True,
     "hint": "Backend-only; use model-gateway :11435 from host. Run: docker compose up -d llamacpp"},
    {"id": "model-gateway", "name": "Model Gateway", "port": 11435, "url": "http://localhost:11435", "check": "http://model-gateway:11435/health/liveliness", "has_gpu": False,
     "hint": "OpenAI-compatible proxy (LiteLLM). Routes inference to llama.cpp."},
    {"id": "webui", "name": "Open WebUI", "port": 3000, "url": "http://localhost:3000", "check": "http://open-webui:8080", "has_gpu": False,
     "hint": "Uses model-gateway for chat. Check: docker compose logs open-webui"},
    {"id": "mcp", "name": "MCP Gateway", "port": 8811, "url": "http://localhost:8811", "check": "http://mcp-gateway:8811/mcp", "has_gpu": False,
     "hint": "Add/remove tools from the dashboard. Connect at http://localhost:8811/mcp — see mcp/README.md"},
    {"id": "comfyui", "name": "ComfyUI", "port": 8188, "url": "http://localhost:8188", "check": "http://comfyui:8188", "has_gpu": True,
     "hint": "ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU). Run ./compose up -d. Pull LTX-2 via dashboard."},
    {"id": "n8n", "name": "N8N", "port": 5678, "url": "http://localhost:5678", "check": "http://n8n:5678", "has_gpu": False,
     "hint": "Check: docker compose logs n8n"},
    {"id": "openclaw", "name": "OpenClaw", "port": int(_OPENCLAW_GATEWAY_PORT), "has_gpu": False,
     "url": f"http://localhost:{_OPENCLAW_GATEWAY_PORT}",
     "check": f"http://openclaw-gateway:{_OPENCLAW_GATEWAY_INTERNAL_PORT}/",
     "hint": (
         f"Control UI: port {_OPENCLAW_GATEWAY_PORT} with ?token=. "
         f"Not :{_OPENCLAW_UI_PORT} (browser/CDP bridge). Logs: docker compose logs openclaw-gateway"
    )},
    {"id": "qdrant", "name": "Qdrant", "port": 6333, "url": "http://localhost:6333", "has_gpu": False,
     "check": "http://qdrant:6333/readyz",
     "hint": "Vector DB for RAG. Drop files in data/rag-input/ (with --profile rag) or upload via Open WebUI Documents tab."},
]


async def _check_service(url: str, client: _httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Check if a service is reachable. Returns (ok, error_message)."""
    try:
        c = client or _httpx.AsyncClient(timeout=3.0)
        try:
            r = await c.get(url)
            return (r.status_code < 500, "")
        finally:
            if client is None:
                await c.aclose()
    except (_httpx.RequestError, OSError) as e:
        err = str(e).lower()
        if "connection refused" in err or "connection reset" in err:
            return (False, str(e))
        if "remoteprotocolerror" in err or "protocol" in err or "closed" in err or "disconnected" in err:
            return (True, "")
        return (False, str(e))
