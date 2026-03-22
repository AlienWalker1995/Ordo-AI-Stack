"""Model Gateway — OpenAI-compatible proxy for Ollama, vLLM, and future providers."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import AsyncClient
from pydantic import BaseModel

app = FastAPI(title="Model Gateway", version="1.0.0")

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex[:8]
    request.state.correlation_id = correlation_id
    if request.url.path not in ("/health", "/ready"):
        service = request.headers.get("X-Service-Name", "")
        logger.info(">>> %s %s from=%s service=%s cid=%s",
                    request.method, request.url.path,
                    request.client.host if request.client else "?",
                    service, correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    if request.url.path not in ("/health", "/ready"):
        logger.info("<<< %s %s status=%s cid=%s",
                    request.method, request.url.path, response.status_code, correlation_id)
    return response

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
VLLM_URL = os.environ.get("VLLM_URL", "").rstrip("/")  # e.g. http://vllm:8000
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "ollama")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
THROUGHPUT_RECORD_TOKEN = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
MODEL_CACHE_TTL = float(os.environ.get("MODEL_CACHE_TTL_SEC", "60"))
# Context window cap. Ollama defaults to a model's max (often 128K+) which pre-allocates
# a huge KV cache even for short prompts — severely hurting CPU throughput.
# Set to 0 to leave Ollama's default unchanged.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
# When Claude Code sends a "claude-*" model name, remap it to this local model.
CLAUDE_CODE_LOCAL_MODEL = os.environ.get("CLAUDE_CODE_LOCAL_MODEL", "")

# TTL model list cache: avoids hitting Ollama on every /v1/models call.
_model_cache: list = []
_model_cache_ts: float = 0.0


def _model_provider_and_id(name: str) -> tuple[str, str]:
    """Return (provider, model_id). Provider: ollama, vllm.
    Only 'vllm/' and 'ollama/' are treated as explicit provider prefixes.
    HF-style paths (hf.co/org/model, org/model) are passed through as-is."""
    if name.startswith("vllm/") and VLLM_URL:
        return ("vllm", name[5:])
    if name.startswith("ollama/"):
        return ("ollama", name[7:])
    return (DEFAULT_PROVIDER, name)


def _ollama_model_id(name: str) -> str:
    """Strip provider prefix if present (ollama/deepseek-r1:7b -> deepseek-r1:7b)."""
    _, model_id = _model_provider_and_id(name)
    return model_id


def _ollama_model_id_with_hf_fallback(name: str) -> str:
    """Resolve Ollama model ID. For org/model names (e.g. unsloth/Qwen3.5-9B-GGUF:latest),
    check the model cache first — if the bare name is known to Ollama (community model),
    use it as-is. Otherwise prepend hf.co/ for HuggingFace GGUF models."""
    model = _ollama_model_id(name)
    model_lower = model.lower()
    # Already fully qualified
    if "/" not in model or model_lower.startswith("hf.co/") or model_lower.startswith("huggingface.co/") or model_lower.startswith("library/"):
        return model
    # Check model cache: if the bare name is already known, don't add hf.co/
    known_ids = {m.get("id", "").lower() for m in _model_cache} if _model_cache else set()
    if model_lower in known_ids or not known_ids:
        # Bare name is known, or cache is empty (try bare name first — Ollama
        # community models like jaahas/... are more common than bare HF paths)
        return model
    # Not in cache — assume HuggingFace
    return f"hf.co/{model}"


def _service_from_headers(origin: str | None, x_service: str | None) -> str:
    """Derive service name from Origin or X-Service-Name header."""
    if x_service and x_service.strip():
        return x_service.strip()[:64]
    if not origin:
        return "unknown"
    o = origin.lower()
    if ":3000" in o or "open-webui" in o:
        return "open-webui"
    if ":5678" in o or "n8n" in o:
        return "n8n"
    if ":8080" in o and "dashboard" not in o:
        return "dashboard"
    if "openclaw" in o or ":6666" in o or ":6667" in o or ":18789" in o or ":18790" in o:
        return "openclaw"
    # Fallback: host:port
    try:
        return origin.replace("http://", "").replace("https://", "").split("/")[0][:64]
    except Exception:
        return "unknown"


def _record_throughput(
    model: str, eval_count: int, eval_duration_ns: int, service: str = ""
) -> None:
    """Fire-and-forget: record throughput to dashboard for real-world stats."""
    if not DASHBOARD_URL or eval_count <= 0 or eval_duration_ns <= 0:
        return
    eval_duration_sec = eval_duration_ns / 1e9
    tps = eval_count / eval_duration_sec

    async def _post():
        try:
            headers = {}
            if THROUGHPUT_RECORD_TOKEN:
                headers["X-Throughput-Token"] = THROUGHPUT_RECORD_TOKEN
            async with AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{DASHBOARD_URL}/api/throughput/record",
                    json={
                        "model": model,
                        "output_tokens_per_sec": round(tps, 1),
                        "service": service or "unknown",
                    },
                    headers=headers if headers else None,
                )
        except Exception:
            pass

    asyncio.create_task(_post())


# --- Models ---


@app.get("/v1/models")
async def list_models():
    """List models in OpenAI format. Aggregates from Ollama and vLLM (when configured).
    Results are cached for MODEL_CACHE_TTL_SEC seconds to reduce Ollama load.
    """
    global _model_cache, _model_cache_ts

    # Serve from cache if still fresh
    if MODEL_CACHE_TTL > 0 and _model_cache and (time.monotonic() - _model_cache_ts) < MODEL_CACHE_TTL:
        return {"object": "list", "data": _model_cache}

    objects = []

    # Ollama
    async with AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            data = r.json()
            for m in data.get("models", []):
                name = m.get("name", "")
                if name:
                    ts = m.get("modified_at", 0) or 0
                    # Expose bare name (e.g. deepseek-r1:7b) so Claude Code and other
                    # clients that validate model names against this list can find them.
                    objects.append({
                        "id": name,
                        "object": "model",
                        "created": ts,
                        "owned_by": "ollama",
                    })
                    # Also expose prefixed form (ollama/name) for provider-aware clients.
                    objects.append({
                        "id": f"ollama/{name}",
                        "object": "model",
                        "created": ts,
                        "owned_by": "ollama",
                    })
        except Exception:
            pass

    # vLLM (OpenAI-compatible /v1/models)
    if VLLM_URL:
        try:
            async with AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{VLLM_URL}/v1/models")
                if r.status_code < 500:
                    data = r.json()
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        if mid:
                            objects.append({
                                "id": f"vllm/{mid}" if "/" not in mid else mid,
                                "object": "model",
                                "created": m.get("created", 0) or 0,
                                "owned_by": "vllm",
                            })
        except Exception:
            pass

    # When CLAUDE_CODE_LOCAL_MODEL is set, advertise claude-* model names so
    # Claude Code's model validation passes before the request is remapped.
    if CLAUDE_CODE_LOCAL_MODEL and objects:
        for alias in ("claude-sonnet-4-5-20250514", "claude-sonnet-4-6-20250725"):
            objects.append({
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": f"local:{CLAUDE_CODE_LOCAL_MODEL}",
            })

    if objects:
        _model_cache = objects
        _model_cache_ts = time.monotonic()
    elif _model_cache:
        # Provider unreachable but we have a stale cache — serve it rather than empty
        return {"object": "list", "data": _model_cache}

    return {"object": "list", "data": objects}


@app.delete("/v1/cache")
async def invalidate_cache():
    """Invalidate the model list cache. Useful after pulling or deleting models."""
    global _model_cache, _model_cache_ts
    _model_cache = []
    _model_cache_ts = 0.0
    return {"ok": True, "message": "Model list cache invalidated"}


# --- Ollama proxy (dashboard model ops — PRD Principle 4: route via gateway) ---


@app.get("/api/tags")
async def ollama_tags():
    """Proxy Ollama /api/tags. Dashboard uses this instead of direct Ollama."""
    async with AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{OLLAMA_URL}/api/tags")
        r.raise_for_status()
        return r.json()


@app.get("/api/ps")
async def ollama_ps():
    """Proxy Ollama /api/ps (loaded models)."""
    async with AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{OLLAMA_URL}/api/ps")
        r.raise_for_status()
        return r.json()


@app.delete("/api/delete")
async def ollama_delete(request: Request):
    """Proxy Ollama /api/delete."""
    body = await request.json()
    async with AsyncClient(timeout=60.0) as client:
        r = await client.request("DELETE", f"{OLLAMA_URL}/api/delete", json=body)
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}


@app.post("/api/pull")
async def ollama_pull(request: Request):
    """Proxy Ollama /api/pull (streaming)."""
    body = await request.json()
    async def stream():
        async with AsyncClient(timeout=3600.0) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/pull", json=body) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/generate")
async def ollama_generate(request: Request):
    """Proxy Ollama /api/generate (non-streaming)."""
    body = await request.json()
    async with AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
        r.raise_for_status()
        return r.json()


@app.post("/api/chat")
async def ollama_chat(request: Request):
    """Proxy Ollama /api/chat (n8n Ollama nodes, etc.)."""
    body = await request.json()
    if body.get("stream"):
        async def stream():
            async with AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=body) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    async with AsyncClient(timeout=300.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
        r.raise_for_status()
        return r.json()


@app.post("/api/embed")
async def ollama_embed(request: Request):
    """Proxy Ollama /api/embed (n8n Embeddings Ollama, etc.)."""
    body = await request.json()
    async with AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/embed", json=body)
        r.raise_for_status()
        return r.json()


@app.get("/")
async def root():
    """Ollama-compatible root endpoint."""
    return "Ollama is running"


@app.get("/api/version")
async def api_version():
    """Proxy Ollama version (Claude Code checks this for compatibility)."""
    try:
        async with AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/version")
            return r.json()
    except Exception:
        return {"version": "0.18.0"}


@app.post("/api/show")
async def api_show(request: Request):
    """Proxy Ollama model info."""
    try:
        raw = await request.json()
        async with AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/show", json=raw)
            return r.json()
    except Exception:
        return {"error": "model not found"}


async def _probe_ollama_l1(client: AsyncClient) -> dict:
    """Reachability + version (L1)."""
    t0 = time.monotonic()
    try:
        r = await client.get(f"{OLLAMA_URL}/api/version")
        ms = round((time.monotonic() - t0) * 1000, 1)
        ok = r.status_code < 500
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {
            "ok": ok,
            "status_code": r.status_code,
            "latency_ms": ms,
            "version": body.get("version"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _probe_vllm_l1(client: AsyncClient) -> dict:
    if not VLLM_URL:
        return {"ok": False, "skipped": True, "reason": "VLLM_URL unset"}
    t0 = time.monotonic()
    try:
        r = await client.get(f"{VLLM_URL}/health")
        ms = round((time.monotonic() - t0) * 1000, 1)
        ok = r.status_code < 500
        return {"ok": ok, "status_code": r.status_code, "latency_ms": ms}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/health")
async def health():
    """L1 gateway health: provider reachability, latency, model-cache freshness hints.

    Use ``/ready`` for L2 (can enumerate models / accept inference routing).
    """
    providers: dict[str, Any] = {}
    ok = False
    async with AsyncClient(timeout=3.0) as client:
        providers["ollama"] = await _probe_ollama_l1(client)
        ok = ok or providers["ollama"].get("ok") is True
        providers["vllm"] = await _probe_vllm_l1(client)
        if not providers["vllm"].get("skipped"):
            ok = ok or providers["vllm"].get("ok") is True

    cache_age = None
    if _model_cache and _model_cache_ts > 0:
        cache_age = round(time.monotonic() - _model_cache_ts, 1)

    return {
        "ok": ok,
        "service": "model-gateway",
        "default_provider": DEFAULT_PROVIDER,
        "providers": providers,
        "model_cache": {
            "ttl_sec": MODEL_CACHE_TTL,
            "entries": len(_model_cache),
            "age_sec": cache_age,
        },
    }


@app.get("/ready")
async def ready():
    """L2 readiness: at least one backend is up **and** at least one model is listed
    (Ollama /api/tags or vLLM /v1/models). Returns HTTP 503 when not ready for inference.
    """
    providers: dict[str, Any] = {}
    any_l1 = False
    model_count = 0

    async with AsyncClient(timeout=5.0) as client:
        providers["ollama"] = await _probe_ollama_l1(client)
        any_l1 = any_l1 or providers["ollama"].get("ok") is True
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code < 500:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                model_count += len(data.get("models") or [])
                providers["ollama"]["tags_ok"] = True
                providers["ollama"]["model_count"] = len(data.get("models") or [])
            else:
                providers["ollama"]["tags_ok"] = False
        except Exception as e:
            providers["ollama"]["tags_ok"] = False
            providers["ollama"]["tags_error"] = str(e)

        providers["vllm"] = await _probe_vllm_l1(client)
        if not providers["vllm"].get("skipped") and providers["vllm"].get("ok"):
            any_l1 = True
            try:
                r2 = await client.get(f"{VLLM_URL}/v1/models")
                if r2.status_code < 500:
                    data2 = r2.json()
                    n = len(data2.get("data") or [])
                    model_count += n
                    providers["vllm"]["model_count"] = n
                    providers["vllm"]["models_ok"] = True
                else:
                    providers["vllm"]["models_ok"] = False
            except Exception as e:
                providers["vllm"]["models_ok"] = False
                providers["vllm"]["models_error"] = str(e)

    # Ready if any provider lists at least one model, or Ollama up with tags (empty = degraded but could still mean fresh install)
    l2_ok = model_count > 0
    degraded = any_l1 and not l2_ok
    ready_flag = any_l1 and l2_ok
    reason = None
    if not any_l1:
        reason = "dependency_unavailable"
    elif degraded:
        reason = "no_models_configured"

    payload = {
        "ready": ready_flag,
        "level": "l2" if ready_flag else ("l1" if any_l1 else "none"),
        "degraded": degraded,
        "reason": reason,
        "model_count": model_count,
        "providers": providers,
        "default_provider": DEFAULT_PROVIDER,
    }

    if not ready_flag and not degraded:
        return JSONResponse(status_code=503, content=payload)
    if degraded:
        payload["ready"] = False
        return JSONResponse(status_code=503, content=payload)
    return payload


# --- Chat ---


def _ollama_to_openai_message(msg: dict) -> dict:
    """Convert Ollama message to OpenAI format."""
    role = msg.get("role", "assistant")
    content = msg.get("content", "")
    if isinstance(content, list):
        # Ollama can return content as list of parts
        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    else:
        text = str(content)
    return {"role": role, "content": text}


class ChatMessage(BaseModel):
    role: str
    content: Any = ""
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: Any = None


class CompletionRequest(BaseModel):
    model: str = ""
    prompt: Any = ""
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: Any = None


class ResponsesRequest(BaseModel):
    model: str = ""
    input: Any = ""
    instructions: str = ""
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stream: bool = False
    max_tokens: int | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: Any = None


class EmbeddingRequest(BaseModel):
    model: str = ""
    input: Any = ""


# Ensure models are fully built when module is loaded dynamically (e.g. in tests)
ChatCompletionRequest.model_rebuild()


def _stream_chunk_openai(obj: dict) -> str:
    """Format OpenAI SSE chunk."""
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Chat completion. Proxies to Ollama or vLLM based on model prefix."""
    body = ChatCompletionRequest.model_validate(await request.json()).model_dump(exclude_none=True)
    return await _chat_completions_impl(request, body)


async def _chat_completions_impl(request: Request, body: dict[str, Any]):
    """Internal implementation: body is already a validated dict."""
    model = body.get("model", "")
    provider, model_id = _model_provider_and_id(model)
    service = _service_from_headers(
        request.headers.get("Origin"),
        request.headers.get("X-Service-Name") or request.headers.get("X-Client-Id"),
    )
    req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    # Remap "developer" role → "system" (Ollama doesn't support developer role)
    messages = [
        {**m, "role": "system"} if m.get("role") == "developer" else m
        for m in body.get("messages", [])
    ]
    stream = body.get("stream", False)

    # vLLM: native OpenAI format — proxy directly
    if provider == "vllm" and VLLM_URL:
        if stream:
            async def vllm_stream():
                async with AsyncClient(timeout=600.0) as client:
                    async with client.stream(
                        "POST",
                        f"{VLLM_URL}/v1/chat/completions",
                        json={**body, "model": model_id},
                        headers={"Content-Type": "application/json"},
                    ) as r:
                        r.raise_for_status()
                        async for chunk in r.aiter_bytes():
                            yield chunk
            return StreamingResponse(
                vllm_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": req_id},
            )
        # vLLM non-streaming
        async with AsyncClient(timeout=600.0) as client:
            r = await client.post(
                f"{VLLM_URL}/v1/chat/completions",
                json={**body, "model": model_id},
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            resp = r.json()
            resp["_request_id"] = req_id
            return resp

    # Ollama: strip provider prefix (ollama/qwen2.5:7b -> qwen2.5:7b)
    ollama_model = _ollama_model_id_with_hf_fallback(model_id)

    def _normalize_for_ollama(msgs: list[dict]) -> list[dict]:
        """Normalize OpenAI-format messages to Ollama-compatible format.

        Key differences:
        - OpenAI tool_calls.function.arguments is a JSON *string*; Ollama expects a *dict*.
        - OpenAI content can be a list of parts; Ollama expects a plain string.
        - assistant messages with only tool_calls may have content=null; Ollama wants content="".
        """
        out = []
        for m in msgs:
            role = m.get("role", "")
            content = m.get("content")

            # Flatten list content → string
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        # text part
                        if p.get("type") in ("text", "output_text") and "text" in p:
                            parts.append(str(p["text"]))
                        # tool_result part (Anthropic-style user message)
                        elif p.get("type") == "tool_result":
                            inner = p.get("content", "")
                            if isinstance(inner, list):
                                inner = "".join(
                                    x.get("text", "") if isinstance(x, dict) else str(x)
                                    for x in inner
                                )
                            parts.append(str(inner))
                        else:
                            # Fallback: any string-like value in the part
                            for k in ("text", "content", "value"):
                                if k in p:
                                    parts.append(str(p[k]))
                                    break
                    elif isinstance(p, str):
                        parts.append(p)
                content = "\n".join(parts)

            # Null content → empty string (Ollama rejects null)
            if content is None:
                content = ""

            msg_out: dict[str, Any] = {"role": role, "content": content}

            # Carry over tool_call_id for tool-result messages
            if "tool_call_id" in m:
                msg_out["tool_call_id"] = m["tool_call_id"]

            # Normalize tool_calls: OpenAI sends arguments as a JSON string; Ollama needs a dict
            raw_tcs = m.get("tool_calls")
            if raw_tcs:
                normalized_tcs = []
                for tc in raw_tcs:
                    func = (tc.get("function") or {})
                    args = func.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, ValueError):
                            args = {}
                    normalized_tcs.append({
                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {"name": func.get("name", ""), "arguments": args},
                    })
                msg_out["tool_calls"] = normalized_tcs

            out.append(msg_out)
        return out

    ollama_body: dict[str, Any] = {"model": ollama_model, "messages": _normalize_for_ollama(messages), "stream": stream}
    # Forward tools — Ollama's /api/chat accepts the same OpenAI tools format
    if body.get("tools"):
        ollama_body["tools"] = body["tools"]
    if "tool_choice" in body:
        ollama_body["tool_choice"] = body["tool_choice"]

    # Ollama performance options.
    # num_ctx: cap the KV cache to avoid Ollama pre-allocating the full model max
    # (often 128K+) even for short prompts — dramatically improves CPU throughput.
    # think=False for Qwen3+tools: suppresses the extended reasoning phase that
    # causes a long silent pause before any output appears, making the request
    # look hung. Tool calls rarely benefit from the extra thinking overhead.
    _opts: dict[str, Any] = {}
    if OLLAMA_NUM_CTX > 0:
        _opts["num_ctx"] = OLLAMA_NUM_CTX
    _lm = ollama_model.lower()
    if ollama_body.get("tools") and ("qwen3" in _lm or "qwen-3" in _lm):
        _opts["think"] = False
    if _opts:
        # Per-request options (if any passed in body) override our defaults.
        ollama_body["options"] = {**_opts, **(ollama_body.get("options") or {})}

    def _ollama_tc_to_openai(tc: dict) -> dict:
        """Convert Ollama tool_call to OpenAI format.
        Ollama arguments are a dict; OpenAI expects a JSON string."""
        func = tc.get("function", {})
        args = func.get("arguments", {})
        return {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": func.get("name", ""),
                "arguments": json.dumps(args) if isinstance(args, dict) else str(args),
            },
        }

    if stream:
        async def generate():
            chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            first_sent = False
            last_eval_count = 0
            last_eval_duration = 0
            pending_tool_calls: list[dict] = []

            async with AsyncClient(timeout=3600.0) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_URL}/api/chat", json=ollama_body
                ) as resp:
                    if resp.status_code >= 400:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")[:300]
                        logger.error("Ollama chat error status=%d body=%s", resp.status_code, err_text)
                        yield _stream_chunk_openai({
                            "id": chunk_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model,
                            "choices": [{"index": 0, "delta": {"role": "assistant",
                                "content": f"[Model error {resp.status_code}: {err_text}]"},
                                "finish_reason": "stop"}],
                        })
                        yield "data: [DONE]\n\n"
                        return
                    try:
                        async for line in resp.aiter_lines():
                            if not line or line == "data: [DONE]":
                                continue
                            try:
                                data = json.loads(line)
                                if data.get("done"):
                                    last_eval_count = data.get("eval_count", 0)
                                    last_eval_duration = data.get("eval_duration", 0)
                                    continue  # done chunk carries no new content
                                msg = data.get("message", {})
                                # Ollama delivers tool_calls in a non-done chunk (not the done chunk)
                                tcs = msg.get("tool_calls")
                                if tcs:
                                    pending_tool_calls = tcs
                                content = msg.get("content", "")
                                if isinstance(content, list):
                                    content = "".join(
                                        p.get("text", "") if isinstance(p, dict) else str(p)
                                        for p in content
                                    )
                                else:
                                    content = str(content)
                                if content:
                                    delta: dict[str, Any] = {"content": content}
                                    if not first_sent:
                                        delta["role"] = "assistant"
                                        first_sent = True
                                    yield _stream_chunk_openai({
                                        "id": chunk_id,
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": model,
                                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                                    })
                            except json.JSONDecodeError:
                                continue
                    except Exception as stream_exc:
                        logger.error("Ollama stream read error: %s", stream_exc)
                        if not first_sent:
                            yield _stream_chunk_openai({
                                "id": chunk_id, "object": "chat.completion.chunk",
                                "created": int(time.time()), "model": model,
                                "choices": [{"index": 0, "delta": {"role": "assistant",
                                    "content": f"[Stream error: {stream_exc}]"},
                                    "finish_reason": "stop"}],
                            })

            if last_eval_count and last_eval_duration:
                _record_throughput(model_id, last_eval_count, last_eval_duration, service)

            # Emit tool calls as OpenAI streaming chunks
            for i, tc in enumerate(pending_tool_calls):
                openai_tc = _ollama_tc_to_openai(tc)
                init_delta: dict[str, Any] = {
                    "tool_calls": [{"index": i, "id": openai_tc["id"], "type": "function",
                                    "function": {"name": openai_tc["function"]["name"], "arguments": ""}}]
                }
                if not first_sent:
                    init_delta["role"] = "assistant"
                    first_sent = True
                yield _stream_chunk_openai({
                    "id": chunk_id, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": init_delta, "finish_reason": None}],
                })
                yield _stream_chunk_openai({
                    "id": chunk_id, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": i, "function": {"arguments": openai_tc["function"]["arguments"]}}]
                    }, "finish_reason": None}],
                })

            finish_reason = "tool_calls" if pending_tool_calls else "stop"
            yield _stream_chunk_openai({
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"delta": {}, "finish_reason": finish_reason}],
            })
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": req_id},
        )

    # Non-streaming
    async with AsyncClient(timeout=600.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=ollama_body)
        r.raise_for_status()
        data = r.json()
    eval_count = data.get("eval_count", 0)
    eval_duration = data.get("eval_duration", 0)
    if eval_count and eval_duration:
        _record_throughput(model_id, eval_count, eval_duration, service)
    msg = data.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    resp_message: dict[str, Any] = {"role": "assistant", "content": content}
    ollama_tool_calls = msg.get("tool_calls") or []
    finish_reason = "stop"
    if ollama_tool_calls:
        resp_message["tool_calls"] = [_ollama_tc_to_openai(tc) for tc in ollama_tool_calls]
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-gateway",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": resp_message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": eval_count,
            "total_tokens": data.get("prompt_eval_count", 0) + eval_count,
        },
    }


# --- Legacy Completions (redirect to chat) ---


@app.post("/v1/completions")
async def completions_compat(request: Request, body: CompletionRequest):
    """Legacy text completions — convert to chat format and proxy."""
    logger.warning(">>> /v1/completions called (legacy); converting to chat format. model=%s", body.model)
    prompt = body.prompt
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    chat_body: dict[str, Any] = {
        "model": body.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": body.stream,
    }
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        v = getattr(body, k, None)
        if v is not None:
            chat_body[k] = v
    return await _chat_completions_impl(request, chat_body)


# --- Responses API (OpenAI Responses format) ---


@app.post("/v1/responses")
async def responses_api(request: Request, body: ResponsesRequest):
    """OpenAI Responses API — convert to chat completions and proxy, preserving tools."""
    raw_tools = body.tools or []

    messages: list[dict] = []
    instructions = body.instructions
    if instructions:
        messages.append({"role": "system", "content": instructions})

    def _content_to_str(c: Any) -> str:
        """Ollama expects content as string; Responses API may send array of parts."""
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and "text" in p:
                    parts.append(str(p["text"]))
                elif isinstance(p, str):
                    parts.append(p)
            return "\n".join(parts) if parts else ""
        return str(c) if c is not None else ""

    # Convert Responses API input items → chat messages.
    # Handles plain messages, function_call (→ assistant tool_calls), and
    # function_call_output (→ tool result message).
    inp = body.input
    if isinstance(inp, str) and inp:
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        idx = 0
        while idx < len(inp):
            item = inp[idx]
            if isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "function_call":
                    # Collect consecutive function_call items → single assistant message
                    tool_calls: list[dict] = []
                    while idx < len(inp) and isinstance(inp[idx], dict) and inp[idx].get("type") == "function_call":
                        fc = inp[idx]
                        # Responses API stores arguments as JSON string; Ollama expects a dict
                        args_raw = fc.get("arguments", "{}")
                        if isinstance(args_raw, str):
                            try:
                                args_dict = json.loads(args_raw)
                            except (json.JSONDecodeError, ValueError):
                                args_dict = {}
                        else:
                            args_dict = args_raw
                        tool_calls.append({
                            "id": fc.get("call_id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": fc.get("name", ""),
                                "arguments": args_dict,
                            },
                        })
                        idx += 1
                    messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
                    continue
                elif item_type == "function_call_output":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("call_id", ""),
                        "content": str(item.get("output", "")),
                    })
                else:
                    role = item.get("role", "user")
                    # Ollama doesn't support "developer" role (OpenAI-only); treat as system.
                    if role == "developer":
                        role = "system"
                    messages.append({"role": role, "content": _content_to_str(item.get("content"))})
            elif isinstance(item, str):
                messages.append({"role": "user", "content": item})
            idx += 1

    # Convert Responses API tool defs → Chat Completions format.
    # Responses API: {type, name, description, parameters}
    # Chat Completions: {type, function: {name, description, parameters}}
    def _convert_tools(resp_tools: list) -> list:
        result = []
        for t in resp_tools:
            if not isinstance(t, dict):
                continue
            if "function" in t:
                result.append(t)  # already in chat format
            elif t.get("type") == "function":
                result.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                })
        return result

    stream = body.stream
    chat_body: dict[str, Any] = {
        "model": body.model,
        "messages": messages,
        "stream": stream,
    }
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        v = getattr(body, k, None)
        if v is not None:
            chat_body[k] = v
    if body.max_output_tokens and "max_tokens" not in chat_body:
        chat_body["max_tokens"] = body.max_output_tokens
    if raw_tools:
        chat_body["tools"] = _convert_tools(raw_tools)
    if body.tool_choice is not None:
        chat_body["tool_choice"] = body.tool_choice

    chat_response = await _chat_completions_impl(request, chat_body)

    if stream or isinstance(chat_response, StreamingResponse):
        async def _to_responses_stream():
            resp_id = f"resp-{uuid.uuid4().hex[:12]}"
            msg_item_id = f"msg-{uuid.uuid4().hex[:12]}"
            seq = 0
            model = body.model

            yield _stream_chunk_openai({
                "type": "response.created",
                "response": {"id": resp_id, "created_at": int(time.time()), "model": model, "status": "in_progress"},
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.output_item.added",
                "item": {"type": "message", "id": msg_item_id, "role": "assistant", "content": []},
                "output_index": 0,
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.content_part.added",
                "content_index": 0,
                "item_id": msg_item_id,
                "output_index": 0,
                "part": {"type": "output_text", "text": ""},
                "sequence_number": seq,
            })
            seq += 1

            buf = ""
            full_text = ""
            # tool_calls_acc: index → {id, name, arguments}
            tool_calls_acc: dict[int, dict] = {}
            tool_call_item_ids: dict[int, str] = {}

            try:
                async for chunk in chat_response.body_iterator:
                    buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        for line in block.strip().split("\n"):
                            line = line.strip()
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                continue
                            try:
                                data = json.loads(line[6:])
                                delta = (data.get("choices") or [{}])[0].get("delta", {})

                                # Text content
                                content = delta.get("content", "")
                                if content:
                                    full_text += content
                                    yield _stream_chunk_openai({
                                        "type": "response.output_text.delta",
                                        "delta": content,
                                        "item_id": msg_item_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "sequence_number": seq,
                                    })
                                    seq += 1

                                # Tool call deltas
                                for tc in (delta.get("tool_calls") or []):
                                    tc_idx = tc.get("index", 0)
                                    if tc_idx not in tool_calls_acc:
                                        call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                                        func_name = (tc.get("function") or {}).get("name", "")
                                        fc_item_id = f"fc-{uuid.uuid4().hex[:12]}"
                                        tool_call_item_ids[tc_idx] = fc_item_id
                                        tool_calls_acc[tc_idx] = {"id": call_id, "name": func_name, "arguments": ""}
                                        yield _stream_chunk_openai({
                                            "type": "response.output_item.added",
                                            "item": {
                                                "type": "function_call",
                                                "id": fc_item_id,
                                                "call_id": call_id,
                                                "name": func_name,
                                                "arguments": "",
                                            },
                                            "output_index": 1 + tc_idx,
                                            "sequence_number": seq,
                                        })
                                        seq += 1
                                    else:
                                        fn = (tc.get("function") or {}).get("name")
                                        if fn:
                                            tool_calls_acc[tc_idx]["name"] = fn

                                    args_delta = (tc.get("function") or {}).get("arguments", "")
                                    if args_delta:
                                        tool_calls_acc[tc_idx]["arguments"] += args_delta
                                        yield _stream_chunk_openai({
                                            "type": "response.function_call_arguments.delta",
                                            "item_id": tool_call_item_ids[tc_idx],
                                            "output_index": 1 + tc_idx,
                                            "delta": args_delta,
                                            "sequence_number": seq,
                                        })
                                        seq += 1
                            except json.JSONDecodeError:
                                pass
            except Exception as exc:
                logger.error("Response stream error: %s", exc)

            # Finalize tool calls
            for tc_idx, tc in sorted(tool_calls_acc.items()):
                fc_item_id = tool_call_item_ids[tc_idx]
                yield _stream_chunk_openai({
                    "type": "response.function_call_arguments.done",
                    "item_id": fc_item_id,
                    "output_index": 1 + tc_idx,
                    "arguments": tc["arguments"],
                    "sequence_number": seq,
                })
                seq += 1
                yield _stream_chunk_openai({
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": fc_item_id,
                        "call_id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                    "output_index": 1 + tc_idx,
                    "sequence_number": seq,
                })
                seq += 1

            # Finalize text message item
            yield _stream_chunk_openai({
                "type": "response.output_text.done",
                "item_id": msg_item_id,
                "output_index": 0,
                "content_index": 0,
                "text": full_text,
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.content_part.done",
                "content_index": 0,
                "item_id": msg_item_id,
                "output_index": 0,
                "part": {"type": "output_text", "text": full_text},
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.output_item.done",
                "item": {"type": "message", "id": msg_item_id, "role": "assistant",
                         "content": [{"type": "output_text", "text": full_text}]},
                "output_index": 0,
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.done",
                "response": {"id": resp_id, "created_at": int(time.time()), "model": model, "status": "completed"},
                "sequence_number": seq,
            })

        return StreamingResponse(
            _to_responses_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Non-streaming response
    resp_id = f"resp-{uuid.uuid4().hex[:12]}"
    choice = chat_response.get("choices", [{}])[0] if isinstance(chat_response, dict) else {}
    msg = choice.get("message", {}) or {}
    content = msg.get("content", "") or ""
    usage = chat_response.get("usage", {}) if isinstance(chat_response, dict) else {}

    output_items: list[dict] = []
    # Tool calls take priority; emit them before any text
    for tc in (msg.get("tool_calls") or []):
        func = tc.get("function", {})
        output_items.append({
            "type": "function_call",
            "id": f"fc-{uuid.uuid4().hex[:12]}",
            "call_id": tc.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", "{}"),
        })
    # Text content — only include the message item if there's content, or if no
    # tool calls were produced (ensures at least one output item).
    if content or not output_items:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content}],
        })

    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": body.model,
        "output": output_items,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "status": "completed",
    }


# --- Anthropic Messages API (Claude Code local model support) ---


def _resolve_claude_model(model: str) -> str:
    """Remap claude-* model names to the configured local model.
    Any other name (e.g. devstral-small-2, qwen2.5-coder:7b) passes through as-is to Ollama."""
    if model.startswith("claude-") and CLAUDE_CODE_LOCAL_MODEL:
        return CLAUDE_CODE_LOCAL_MODEL
    return model


def _anthropic_content_to_str(content: Any) -> str:
    """Flatten Anthropic content (str or list of blocks) to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content) if content else ""


def _anthropic_messages_to_openai(system: Any, messages: list[dict]) -> list[dict]:
    """Convert Anthropic messages array (with content blocks) to OpenAI format."""
    result: list[dict] = []
    if system:
        result.append({"role": "system", "content": _anthropic_content_to_str(system)})
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            result.append({"role": role, "content": content})
        elif isinstance(content, list):
            tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if tool_results:
                for tr in tool_results:
                    tc = tr.get("content", "")
                    if isinstance(tc, list):
                        tc = " ".join(b.get("text", "") for b in tc if isinstance(b, dict))
                    result.append({"role": "tool", "tool_call_id": tr.get("tool_use_id", ""), "content": str(tc)})
            elif tool_uses:
                tool_calls = [
                    {
                        "id": tu.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {"name": tu.get("name", ""), "arguments": json.dumps(tu.get("input", {}))},
                    }
                    for tu in tool_uses
                ]
                text = " ".join(b.get("text", "") for b in text_blocks)
                result.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})
            else:
                result.append({"role": role, "content": " ".join(b.get("text", "") for b in text_blocks)})
        else:
            result.append({"role": role, "content": str(content) if content else ""})
    return result


def _anthropic_tools_to_openai(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t.get("name", ""), "description": t.get("description", ""), "parameters": t.get("input_schema", {})}}
        for t in tools
    ]


def _openai_finish_to_anthropic(finish_reason: str) -> str:
    return {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish_reason, "end_turn")


def _openai_message_to_anthropic_content(message: dict) -> list[dict]:
    blocks: list[dict] = []
    text = message.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in (message.get("tool_calls") or []):
        func = tc.get("function", {})
        args_raw = func.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except (json.JSONDecodeError, ValueError):
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"), "name": func.get("name", ""), "input": args})
    return blocks or [{"type": "text", "text": ""}]


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages API — translates to OpenAI/Ollama format and back.
    Allows Claude Code and other Anthropic-SDK clients to use local models via this gateway.
    Set ANTHROPIC_BASE_URL=http://model-gateway:11435 and ANTHROPIC_API_KEY=local.
    """
    raw = await request.json()
    model = _resolve_claude_model(raw.get("model", ""))
    stream = raw.get("stream", False)
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    ollama_messages = _anthropic_messages_to_openai(raw.get("system"), raw.get("messages", []))
    openai_tools = _anthropic_tools_to_openai(raw["tools"]) if raw.get("tools") else None

    chat_body: dict[str, Any] = {"model": model, "messages": ollama_messages, "stream": stream}
    if raw.get("max_tokens"):
        chat_body["max_tokens"] = raw["max_tokens"]
    if raw.get("temperature") is not None:
        chat_body["temperature"] = raw["temperature"]
    if raw.get("top_p") is not None:
        chat_body["top_p"] = raw["top_p"]
    if raw.get("stop_sequences"):
        chat_body["stop"] = raw["stop_sequences"]
    if openai_tools:
        chat_body["tools"] = openai_tools
    if raw.get("tool_choice") is not None:
        tc = raw["tool_choice"]
        if isinstance(tc, dict):
            t = tc.get("type", "auto")
            chat_body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}} if t == "tool" else ("required" if t == "any" else t)
        else:
            chat_body["tool_choice"] = tc

    if stream:
        async def anthropic_stream():
            openai_resp = await _chat_completions_impl(request, chat_body)
            if not isinstance(openai_resp, StreamingResponse):
                return

            def _sse(event: str, data: dict) -> str:
                return f"event: {event}\ndata: {json.dumps(data)}\n\n"

            yield _sse("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
            yield _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            yield _sse("ping", {"type": "ping"})

            buf = ""
            stop_reason = "end_turn"
            tool_calls_acc: dict[int, dict] = {}
            tool_block_idx: dict[int, int] = {}
            next_block = 1

            try:
                async for chunk in openai_resp.body_iterator:
                    buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        for line in block.strip().split("\n"):
                            line = line.strip()
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                continue
                            try:
                                data = json.loads(line[6:])
                                choice = (data.get("choices") or [{}])[0]
                                delta = choice.get("delta", {})
                                if choice.get("finish_reason"):
                                    stop_reason = _openai_finish_to_anthropic(choice["finish_reason"])
                                text = delta.get("content", "")
                                if text:
                                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                                for tc in (delta.get("tool_calls") or []):
                                    tc_idx = tc.get("index", 0)
                                    if tc_idx not in tool_calls_acc:
                                        call_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"
                                        name = (tc.get("function") or {}).get("name", "")
                                        bi = next_block
                                        next_block += 1
                                        tool_block_idx[tc_idx] = bi
                                        tool_calls_acc[tc_idx] = {"id": call_id, "name": name, "arguments": ""}
                                        yield _sse("content_block_start", {"type": "content_block_start", "index": bi, "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}})
                                    args_delta = (tc.get("function") or {}).get("arguments", "")
                                    if args_delta:
                                        tool_calls_acc[tc_idx]["arguments"] += args_delta
                                        yield _sse("content_block_delta", {"type": "content_block_delta", "index": tool_block_idx[tc_idx], "delta": {"type": "input_json_delta", "partial_json": args_delta}})
                            except json.JSONDecodeError:
                                pass
            except Exception as exc:
                logger.error("Anthropic stream translation error: %s", exc)

            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            for tc_idx in sorted(tool_calls_acc):
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": tool_block_idx[tc_idx]})
            yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}})
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(anthropic_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Non-streaming
    openai_resp = await _chat_completions_impl(request, chat_body)
    choice = (openai_resp.get("choices") or [{}])[0] if isinstance(openai_resp, dict) else {}
    message = choice.get("message", {})
    usage = openai_resp.get("usage", {}) if isinstance(openai_resp, dict) else {}
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": _openai_message_to_anthropic_content(message),
        "model": model,
        "stop_reason": _openai_finish_to_anthropic(choice.get("finish_reason", "stop")),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    }


# --- Embeddings ---


@app.post("/v1/embeddings")
async def embeddings(body: EmbeddingRequest):
    """Embeddings. Proxies to Ollama or vLLM based on model prefix."""
    model = body.model
    provider, model_id = _model_provider_and_id(model)
    inp = body.input

    if isinstance(inp, str):
        inp = [inp]
    if not inp:
        return {"object": "list", "data": [], "model": model}

    # vLLM: native OpenAI format
    if provider == "vllm" and VLLM_URL:
        async with AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{VLLM_URL}/v1/embeddings",
                json={**body.model_dump(exclude_none=True), "model": model_id},
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    # Ollama: strip provider prefix (ollama/qwen2.5:7b -> qwen2.5:7b)
    ollama_model = _ollama_model_id_with_hf_fallback(model_id)
    ollama_body = {"model": ollama_model, "input": inp}
    async with AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/embed", json=ollama_body)
        r.raise_for_status()
        data = r.json()

    embeds = data.get("embeddings", [])
    objects = []
    for i, emb in enumerate(embeds):
        objects.append({
            "object": "embedding",
            "embedding": emb,
            "index": i,
        })
    return {"object": "list", "data": objects, "model": model}
