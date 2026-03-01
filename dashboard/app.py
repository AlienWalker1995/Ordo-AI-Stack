"""AI-toolkit Dashboard — unified model management and service hub."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient
from pydantic import BaseModel

app = FastAPI(title="AI-toolkit Dashboard", version="1.0.0")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))

# Background ComfyUI pull status
_comfyui_status: dict = {"running": False, "output": "", "done": False, "success": None}


class PullRequest(BaseModel):
    model: str


# --- Ollama ---


# Ollama library — models available at registry.ollama.ai (no public API, so we maintain a curated list)
OLLAMA_LIBRARY = [
    "llama3.2", "llama3.1", "llama3", "llama2", "llama4",
    "deepseek-r1:7b", "deepseek-r1:70b", "deepseek-coder:6.7b", "deepseek-coder-v2",
    "deepseek-v3", "deepseek-v3.1", "deepseek-v3.2", "deepseek-v2", "deepseek-llm",
    "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:72b", "qwen2.5-coder", "qwen2.5vl",
    "qwen3", "qwen3.5", "qwen3-coder", "qwen3-vl", "qwen3-next", "qwen3-embedding",
    "qwen2", "qwen2-math", "qwen", "codeqwen",
    "gemma3", "gemma2:9b", "gemma2:27b", "gemma", "embeddinggemma",
    "mistral", "mistral-nemo", "mistral-large", "mistral-small", "mistral-small3.1", "mistral-small3.2",
    "mixtral", "codestral", "ministral-3",
    "phi3", "phi3.5", "phi4", "phi4-mini", "phi4-reasoning", "phi4-mini-reasoning", "phi",
    "nomic-embed-text", "nomic-embed-text-v2-moe", "mxbai-embed-large", "bge-m3", "bge-large",
    "snowflake-arctic-embed", "snowflake-arctic-embed2", "granite-embedding", "paraphrase-multilingual",
    "codellama", "starcoder", "starcoder2", "sqlcoder", "wizardcoder", "magicoder", "codegemma",
    "llava", "llava-llama3", "llava-phi3", "bakllava", "minicpm-v", "moondream",
    "tinyllama", "smollm2", "smollm", "all-minilm", "dolphin3", "dolphin-phi", "dolphin-llama3",
    "dolphin-mixtral", "dolphin-mistral", "dolphincoder", "tinydolphin",
    "olmo2", "olmo-3", "olmo-3.1", "yi", "yi-coder", "glm4", "glm-4.6", "glm-4.7", "glm-4.7-flash",
    "glm-5", "glm-ocr", "minimax-m2", "minimax-m2.1", "minimax-m2.5", "kimi-k2", "kimi-k2.5",
    "kimi-k2-thinking", "granite3.1-moe", "granite3.2", "granite3.2-vision", "granite3.3",
    "granite3.1-dense", "granite3-dense", "granite4", "granite-code", "granite3-guardian",
    "command-r", "command-r7b", "command-r-plus", "command-a", "command-r7b-arabic",
    "devstral", "devstral-small-2", "devstral-2", "codestral",
    "gpt-oss", "gpt-oss-safeguard", "cogito", "cogito-2.1", "gemini-3-flash-preview",
    "nexusraven", "firefunction-v2", "llama3-groq-tool-use", "llama-guard3",
    "wizardlm2", "wizardlm", "wizard-math", "wizard-vicuna", "wizard-vicuna-uncensored",
    "internlm2", "exaone-deep", "exaone3.5", "aya", "aya-expanse",
    "falcon", "falcon2", "falcon3", "solar", "solar-pro", "vicuna", "openchat",
    "nous-hermes", "nous-hermes2", "nous-hermes2-mixtral", "openhermes", "neural-chat",
    "orca-mini", "orca2", "stable-beluga", "stablelm2", "stablelm-zephyr", "stable-code",
    "xwinlm", "llama2-chinese", "llama3-chatqa", "llama-pro", "yarn-llama2", "yarn-mistral",
    "phind-codellama", "opencoder", "openthinker", "deepcoder", "qwq",
    "llama2-uncensored", "everythinglm", "reflection", "meditron", "medllama2",
    "samantha-mistral", "r1-1776", "athene-v2", "nemotron", "nemotron-mini", "nemotron-3-nano",
    "dbrx", "goliath", "megadolphin", "alfred", "marco-o1", "sailor2",
    "smallthinker", "deepseek-v2.5", "phi4-mini-reasoning", "shieldgemma",
    "reader-lm", "qwen3-next", "translategemma", "functiongemma",
    "duckdb-nsql", "nuextract", "mistrallite", "bespoke-minicheck", "tulu3",
    "notux", "notus", "codebooga", "open-orca-platypus2", "codeup", "mathstral",
    "deepseek-ocr", "solar-pro", "rnj-1", "hermes3", "zephyr",
]


@app.get("/api/ollama/library")
async def ollama_library():
    """List models available in the Ollama registry (curated)."""
    return {"models": sorted(set(OLLAMA_LIBRARY)), "ok": True}


@app.get("/api/ollama/models")
async def ollama_models():
    """List models available in Ollama."""
    async with AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            data = r.json()
            return {"models": data.get("models", []), "ok": True}
        except Exception as e:
            return {"models": [], "ok": False, "error": str(e)}


@app.post("/api/ollama/pull")
async def ollama_pull(req: PullRequest):
    """Stream Ollama model pull progress."""
    async def stream():
        async with AsyncClient(timeout=3600.0) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/pull",
                json={"model": req.model, "stream": True},
            ) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- ComfyUI ---


def _scan_comfyui_models() -> list[dict]:
    """Scan ComfyUI models directory for installed files."""
    subdirs = ("checkpoints", "loras", "text_encoders", "latent_upscale_models")
    models = []
    for sub in subdirs:
        d = MODELS_DIR / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                size_mb = f.stat().st_size / (1024 * 1024)
                models.append(
                    {
                        "name": f.name,
                        "category": sub,
                        "size_mb": round(size_mb, 1),
                    }
                )
    return sorted(models, key=lambda m: (m["category"], m["name"]))


def _run_comfyui_pull():
    """Run ComfyUI model pull script in background."""
    global _comfyui_status
    _comfyui_status = {"running": True, "output": "", "done": False, "success": None}
    script = SCRIPTS_DIR / "comfyui" / "pull_comfyui_models.py"
    env = os.environ.copy()
    env["MODELS_DIR"] = str(MODELS_DIR)
    try:
        proc = subprocess.Popen(
            ["python3", str(script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(SCRIPTS_DIR.parent),
        )
        output_lines = []
        for line in proc.stdout:
            output_lines.append(line)
            _comfyui_status["output"] = "".join(output_lines)
        proc.wait()
        _comfyui_status["success"] = proc.returncode == 0
    except Exception as e:
        _comfyui_status["output"] += f"\nError: {e}"
        _comfyui_status["success"] = False
    finally:
        _comfyui_status["running"] = False
        _comfyui_status["done"] = True


@app.get("/api/comfyui/models")
async def comfyui_models():
    """List ComfyUI models on disk."""
    try:
        models = _scan_comfyui_models()
        return {"models": models, "ok": True}
    except Exception as e:
        return {"models": [], "ok": False, "error": str(e)}


@app.post("/api/comfyui/pull")
async def comfyui_pull():
    """Start ComfyUI model pull (LTX-2) in background."""
    global _comfyui_status
    if _comfyui_status.get("running"):
        raise HTTPException(status_code=409, detail="Pull already in progress")
    thread = threading.Thread(target=_run_comfyui_pull)
    thread.daemon = True
    thread.start()
    return {"status": "started", "message": "ComfyUI model pull started. Poll /api/comfyui/pull/status for progress."}


@app.get("/api/comfyui/pull/status")
async def comfyui_pull_status():
    """Get ComfyUI pull progress."""
    return _comfyui_status


# --- Services ---

# Map dashboard service id -> ops-controller service id
OPS_SERVICE_MAP = {
    "ollama": "ollama",
    "model-gateway": "model-gateway",
    "webui": "open-webui",
    "mcp": "mcp-gateway",
    "comfyui": "comfyui",
    "n8n": "n8n",
    "openclaw": "openclaw-gateway",
}

SERVICES = [
    {"id": "ollama", "name": "Ollama", "port": 11434, "url": "http://localhost:11434", "check": "http://ollama:11434/api/version",
     "hint": "Run: docker compose up -d ollama"},
    {"id": "model-gateway", "name": "Model Gateway", "port": 11435, "url": "http://localhost:11435", "check": "http://model-gateway:11435/health",
     "hint": "OpenAI-compatible proxy. Set OPENAI_API_BASE to use."},
    {"id": "webui", "name": "Open WebUI", "port": 3000, "url": "http://localhost:3000", "check": "http://open-webui:8080",
     "hint": "Depends on Ollama. Check: docker compose logs open-webui"},
    {"id": "mcp", "name": "MCP Gateway", "port": 8811, "url": "http://localhost:8811", "check": "http://mcp-gateway:8811/mcp",
     "hint": "Add/remove tools from the dashboard. Connect at http://localhost:8811/mcp — see mcp/README.md"},
    {"id": "comfyui", "name": "ComfyUI", "port": 8188, "url": "http://localhost:8188", "check": "http://comfyui:8188",
     "hint": "ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU). Run ./compose up -d. Pull LTX-2 via dashboard."},
    {"id": "n8n", "name": "N8N", "port": 5678, "url": "http://localhost:5678", "check": "http://n8n:5678",
     "hint": "Check: docker compose logs n8n"},
    {"id": "openclaw", "name": "OpenClaw", "port": 18789, "url": "http://localhost:18789",
     "check": "http://host.docker.internal:18789/",
     "hint": "Run ensure_dirs.ps1 (Windows) or ensure_dirs.sh (Linux/Mac) to create openclaw/.env. Check: docker compose logs openclaw-gateway"},
]


async def _check_service(url: str) -> tuple[bool, str]:
    """Check if a service is reachable. Returns (ok, error_message)."""
    try:
        async with AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
            return (r.status_code < 500, "")
    except Exception as e:
        err = str(e).lower()
        # Connection refused = service down
        if "connection refused" in err or "connection reset" in err:
            return (False, str(e))
        # RemoteProtocolError, connection closed/disconnected = we reached the server (e.g. WebSocket gateway)
        if "remoteprotocolerror" in err or "protocol" in err or "closed" in err or "disconnected" in err:
            return (True, "")
        return (False, str(e))


MCP_GATEWAY_SERVERS = os.environ.get("MCP_GATEWAY_SERVERS", "duckduckgo")
MCP_CONFIG_PATH = os.environ.get("MCP_CONFIG_PATH")
# Suggested servers (dropdown). Users can also add any valid server name via custom input.
MCP_CATALOG = [
    "duckduckgo", "fetch", "dockerhub", "github-official", "brave", "playwright",
    "mongodb", "postgres", "stripe", "notion", "grafana", "elasticsearch",
    "documentation", "perplexity", "excalidraw", "miro", "neo4j",
    "time", "slack", "filesystem", "puppeteer", "context7", "memory",
    "firecrawl", "github", "git", "atlassian", "obsidian", "n8n",
    "hugging-face",
]


def _mcp_config_path() -> Path | None:
    """Path to MCP servers config file (when dashboard has volume mounted)."""
    if not MCP_CONFIG_PATH:
        return None
    p = Path(MCP_CONFIG_PATH)
    return p if p.parent.exists() else None


def _normalize_server(s: str) -> str:
    """Parse URL to server ID, or return as-is if already valid."""
    parsed = _parse_mcp_server_input(s)
    return parsed if parsed else s


def _read_mcp_servers() -> list[str]:
    """Read enabled servers from config file or env. Normalizes URLs to server IDs and deduplicates."""
    path = _mcp_config_path()
    if path:
        if path.exists():
            raw = path.read_text().strip().replace("\r", "").replace("\n", ",")
            raw_list = [s.strip() for s in raw.split(",") if s.strip()]
            normalized = []
            seen = set()
            for s in raw_list:
                n = _normalize_server(s)
                if n and n not in seen:
                    normalized.append(n)
                    seen.add(n)
            # Persist cleanup if we changed anything (URLs → IDs)
            if normalized != raw_list:
                _write_mcp_servers(normalized)
            return normalized
        # Migrate: init file from .env on first run
        path.parent.mkdir(parents=True, exist_ok=True)
        initial = ",".join(s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()) or "duckduckgo"
        path.write_text(initial)
        return [s.strip() for s in initial.split(",") if s.strip()]
    return [s.strip() for s in MCP_GATEWAY_SERVERS.split(",") if s.strip()]


def _write_mcp_servers(servers: list[str]) -> Path:
    """Write servers to config file. Raises if not in dynamic mode."""
    path = _mcp_config_path()
    if not path:
        raise HTTPException(status_code=409, detail="MCP config not in dynamic mode (no volume)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(servers))
    return path


def _mcp_registry_path() -> Path | None:
    """Path to MCP registry.json (optional metadata)."""
    if not MCP_CONFIG_PATH:
        return None
    p = Path(MCP_CONFIG_PATH).parent / "registry.json"
    return p if p.parent.exists() else None


def _read_mcp_registry() -> dict:
    """Read registry.json if present. Falls back to empty dict."""
    path = _mcp_registry_path()
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"servers": {}}


@app.get("/api/mcp/servers")
async def mcp_servers():
    """List enabled MCP servers and catalog for adding."""
    servers = _read_mcp_servers()
    dynamic = _mcp_config_path() is not None
    registry = _read_mcp_registry()
    return {"enabled": servers, "catalog": MCP_CATALOG, "dynamic": dynamic, "registry": registry, "ok": True}


class McpAddRequest(BaseModel):
    server: str


class McpRemoveRequest(BaseModel):
    server: str


def _valid_mcp_server_name(name: str) -> bool:
    """Allow alphanumeric, hyphens, underscores, slashes, colons (Docker refs)."""
    if not name or len(name) > 200:
        return False
    return all(c.isalnum() or c in "-_/:." for c in name)


def _parse_mcp_server_input(raw: str) -> str | None:
    """Extract server ID from input. Accepts:
    - Docker Hub URL: https://hub.docker.com/mcp/server/hugging-face/overview
    - Raw server name: hugging-face, fetch, mcp/firecrawl
    """
    s = raw.strip()
    if not s:
        return None
    # Docker Hub MCP URL: hub.docker.com/mcp/server/<server-id>/...
    if "hub.docker.com" in s and "/mcp/server/" in s:
        try:
            # Extract segment after /mcp/server/
            idx = s.find("/mcp/server/")
            if idx >= 0:
                rest = s[idx + len("/mcp/server/"):]
                server_id = rest.split("/")[0].split("?")[0]
                if server_id and _valid_mcp_server_name(server_id):
                    return server_id
        except (IndexError, ValueError):
            pass
    return s if _valid_mcp_server_name(s) else None


@app.post("/api/mcp/add")
async def mcp_add(req: McpAddRequest):
    """Add an MCP server. Takes effect in ~10s without container restart.
    Accepts: server name (fetch, hugging-face), Docker ref (mcp/firecrawl),
    or Docker Hub URL (https://hub.docker.com/mcp/server/hugging-face/overview)."""
    server = _parse_mcp_server_input(req.server)
    if not server:
        raise HTTPException(status_code=400, detail="Invalid server name or URL. Use a name (e.g. hugging-face) or paste a Docker Hub MCP URL.")
    servers = _read_mcp_servers()
    if server in servers:
        return {"status": "already_enabled", "servers": servers}
    servers.append(server)
    _write_mcp_servers(servers)
    return {"status": "added", "servers": servers}


@app.post("/api/mcp/remove")
async def mcp_remove(req: McpRemoveRequest):
    """Remove an MCP server. Takes effect in ~10s without container restart."""
    server = _parse_mcp_server_input(req.server) or req.server.strip()
    if not server:
        raise HTTPException(status_code=400, detail="Server name required")
    servers = _read_mcp_servers()
    if server not in servers:
        return {"status": "already_removed", "servers": servers}
    servers = [s for s in servers if s != server]
    if not servers:
        raise HTTPException(status_code=400, detail="Cannot remove last server. Add another first.")
    _write_mcp_servers(servers)
    return {"status": "removed", "servers": servers}


@app.get("/api/services")
async def services():
    """Service links and live health status."""
    results = []
    for svc in SERVICES:
        ok, err = await _check_service(svc["check"]) if svc.get("check") else (None, "")
        results.append({
            **{k: v for k, v in svc.items() if k != "check"},
            "ok": ok,
            "error": err if not ok else None,
            "hint": svc.get("hint", ""),
        })
    return {"services": results}


@app.get("/api/health")
async def health():
    """Aggregated platform health. Returns ok=true when all services are reachable."""
    results = []
    for svc in SERVICES:
        ok, err = await _check_service(svc["check"]) if svc.get("check") else (None, "")
        results.append({"id": svc["id"], "ok": ok, "error": err})
    all_ok = all(r["ok"] for r in results if r["ok"] is not None)
    return {"ok": all_ok, "services": results}


# --- Token Throughput ---

# In-memory store: model -> list of output_tokens_per_sec (rolling, max 500)
_throughput_samples: dict[str, list[float]] = {}
_MAX_SAMPLES_PER_MODEL = 500

# Service usage: list of { model, service, tps, ts } for "which service uses which model"
_service_usage: list[dict] = []
_MAX_SERVICE_USAGE = 500


def _percentile(sorted_arr: list[float], p: float) -> float:
    """Compute percentile (0–100). Returns 0 if empty."""
    if not sorted_arr:
        return 0.0
    k = (len(sorted_arr) - 1) * (p / 100)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_arr) else f
    return sorted_arr[f] + (k - f) * (sorted_arr[c] - sorted_arr[f]) if c > f else sorted_arr[f]


class ThroughputBenchmarkRequest(BaseModel):
    model: str = ""


class ThroughputRecordRequest(BaseModel):
    model: str = ""
    output_tokens_per_sec: float = 0.0
    service: str = ""


@app.post("/api/throughput/record")
async def throughput_record(req: ThroughputRecordRequest):
    """Record a throughput sample from real-world usage (e.g. model gateway). Fire-and-forget."""
    model = req.model.strip()
    if not model or req.output_tokens_per_sec <= 0:
        return {"ok": True}
    if model not in _throughput_samples:
        _throughput_samples[model] = []
    _throughput_samples[model].append(req.output_tokens_per_sec)
    if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
        _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]
    # Service usage (which service is taxing which model)
    service = (req.service or "unknown").strip()[:64]
    _service_usage.append({
        "model": model,
        "service": service,
        "tps": round(req.output_tokens_per_sec, 1),
        "ts": time.time(),
    })
    if len(_service_usage) > _MAX_SERVICE_USAGE:
        _service_usage[:] = _service_usage[-_MAX_SERVICE_USAGE:]
    return {"ok": True}


@app.get("/api/throughput/service-usage")
async def throughput_service_usage():
    """Return recent service usage: which service used which model (from model gateway traffic)."""
    now = time.time()
    # Last 24h, grouped by model -> services
    recent = [u for u in _service_usage if (now - u["ts"]) < 86400]
    by_model: dict[str, list[dict]] = {}
    for u in recent:
        m = u["model"]
        if m not in by_model:
            by_model[m] = []
        by_model[m].append({
            "service": u["service"],
            "tps": u["tps"],
            "ts": u["ts"],
        })
    # Per model: unique services, last activity, last tps per service
    result: dict[str, dict] = {}
    for model, usages in by_model.items():
        by_svc: dict[str, list] = {}
        for u in usages:
            s = u["service"]
            if s not in by_svc:
                by_svc[s] = []
            by_svc[s].append({"tps": u["tps"], "ts": u["ts"]})
        result[model] = {
            "services": [
                {
                    "name": svc,
                    "last_tps": max(u["tps"] for u in vals),
                    "last_ts": max(u["ts"] for u in vals),
                    "count": len(vals),
                }
                for svc, vals in by_svc.items()
            ],
        }
    return {"by_model": result, "ok": True}


@app.get("/api/throughput/stats")
async def throughput_stats():
    """Return per-model throughput stats: peak, p50, p95, p99, latest, sample_count."""
    result: dict[str, dict] = {}
    for model, samples in list(_throughput_samples.items()):
        if not samples:
            continue
        sorted_s = sorted(samples)
        result[model] = {
            "latest": round(samples[-1], 1),
            "peak": round(max(samples), 1),
            "p50": round(_percentile(sorted_s, 50), 1),
            "p95": round(_percentile(sorted_s, 95), 1),
            "p99": round(_percentile(sorted_s, 99), 1),
            "sample_count": len(samples),
        }
    return {"models": result, "ok": True}


@app.get("/api/ollama/ps")
async def ollama_ps():
    """List models currently loaded in Ollama (from /api/ps)."""
    async with AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL.rstrip('/')}/api/ps")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}")


@app.post("/api/throughput/benchmark")
async def throughput_benchmark(req: ThroughputBenchmarkRequest):
    """Run a quick benchmark against Ollama. Returns tokens/sec and related metrics."""
    model = req.model.strip() or "llama3.2"
    prompt = "Say 'ok' and nothing else."
    async with AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}")

    eval_count = data.get("eval_count", 0)
    eval_duration_ns = data.get("eval_duration", 0) or 1
    prompt_eval_count = data.get("prompt_eval_count", 0)
    prompt_eval_duration_ns = data.get("prompt_eval_duration", 0) or 1
    load_duration_ns = data.get("load_duration", 0)
    total_duration_ns = data.get("total_duration", 0)

    eval_duration_sec = eval_duration_ns / 1e9
    prompt_eval_duration_sec = prompt_eval_duration_ns / 1e9

    output_tokens_per_sec = eval_count / eval_duration_sec if eval_duration_sec > 0 else 0
    input_tokens_per_sec = prompt_eval_count / prompt_eval_duration_sec if prompt_eval_duration_sec > 0 else 0

    # Store sample for stats (peak, percentiles)
    if model not in _throughput_samples:
        _throughput_samples[model] = []
    _throughput_samples[model].append(output_tokens_per_sec)
    if len(_throughput_samples[model]) > _MAX_SAMPLES_PER_MODEL:
        _throughput_samples[model] = _throughput_samples[model][-_MAX_SAMPLES_PER_MODEL:]

    return {
        "ok": True,
        "model": model,
        "prompt_tokens": prompt_eval_count,
        "output_tokens": eval_count,
        "output_tokens_per_sec": round(output_tokens_per_sec, 1),
        "input_tokens_per_sec": round(input_tokens_per_sec, 1),
        "eval_duration_ms": round(eval_duration_ns / 1e6, 1),
        "load_duration_ms": round(load_duration_ns / 1e6, 1),
        "total_duration_ms": round(total_duration_ns / 1e6, 1),
    }


# --- Ops Controller proxy ---

OPS_CONTROLLER_URL = os.environ.get("OPS_CONTROLLER_URL", "http://ops-controller:9000")
OPS_CONTROLLER_TOKEN = os.environ.get("OPS_CONTROLLER_TOKEN", "")


async def _ops_request(method: str, path: str, **kwargs) -> tuple[int, dict]:
    """Proxy request to ops controller. Returns (status_code, json_body)."""
    if not OPS_CONTROLLER_TOKEN:
        return 503, {"detail": "OPS_CONTROLLER_TOKEN not configured"}
    url = f"{OPS_CONTROLLER_URL.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {OPS_CONTROLLER_TOKEN}", **kwargs.pop("headers", {})}
    try:
        async with AsyncClient(timeout=30.0) as client:
            r = await client.request(method, url, headers=headers, **kwargs)
            try:
                data = r.json()
            except Exception:
                data = {"detail": r.text or "Unknown error"}
            return r.status_code, data
    except Exception as e:
        return 503, {"detail": str(e)}


@app.post("/api/ops/services/{service_id}/restart")
async def ops_restart(service_id: str):
    """Restart a service via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request("POST", f"/services/{ops_id}/restart", json={"confirm": True})
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.get("/api/ops/services/{service_id}/logs")
async def ops_logs(service_id: str, tail: int = 100):
    """Get service logs via ops controller."""
    ops_id = OPS_SERVICE_MAP.get(service_id, service_id)
    code, data = await _ops_request("GET", f"/services/{ops_id}/logs?tail={tail}")
    if code >= 400:
        raise HTTPException(status_code=code, detail=data.get("detail", data))
    return data


@app.get("/api/ops/available")
async def ops_available():
    """Check if ops controller is configured and reachable."""
    if not OPS_CONTROLLER_TOKEN:
        return {"available": False, "reason": "OPS_CONTROLLER_TOKEN not set"}
    code, _ = await _ops_request("GET", "/health")
    return {"available": code == 200}


# --- Static ---

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
