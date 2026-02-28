"""LLM-toolkit Dashboard — unified model management and service hub."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient
from pydantic import BaseModel

app = FastAPI(title="LLM-toolkit Dashboard", version="1.0.0")

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

SERVICES = [
    {"id": "ollama", "name": "Ollama", "port": 11434, "url": "http://localhost:11434", "check": "http://ollama:11434/api/version",
     "hint": "Run: docker compose up -d ollama"},
    {"id": "webui", "name": "Open WebUI", "port": 3000, "url": "http://localhost:3000", "check": "http://open-webui:8080",
     "hint": "Depends on Ollama. Check: docker compose logs open-webui"},
    {"id": "mcp", "name": "MCP Gateway", "port": 8811, "url": "http://localhost:8811", "check": "http://mcp-gateway:8811/mcp",
     "hint": "Add MCP_GATEWAY_SERVERS in .env. Connect at http://localhost:8811/mcp — see mcp/README.md"},
    {"id": "comfyui", "name": "ComfyUI", "port": 8188, "url": "http://localhost:8188", "check": "http://comfyui:8188",
     "hint": "Requires NVIDIA GPU. No GPU? Remove deploy block from docker-compose. Pull LTX-2 via dashboard."},
    {"id": "n8n", "name": "N8N", "port": 5678, "url": "http://localhost:5678", "check": "http://n8n:5678",
     "hint": "Check: docker compose logs n8n"},
    {"id": "openclaw", "name": "OpenClaw", "port": 18789, "url": "http://localhost:18789", "check": "http://openclaw-gateway:18789",
     "hint": "Run ensure_dirs.ps1 to create openclaw/.env. Check: docker compose logs openclaw-gateway"},
]


async def _check_service(url: str) -> tuple[bool, str]:
    """Check if a service is reachable. Returns (ok, error_message)."""
    try:
        async with AsyncClient(timeout=3.0) as client:
            r = await client.get(url)
            return (r.status_code < 500, "")
    except Exception as e:
        return (False, str(e))


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


# --- Static ---

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
