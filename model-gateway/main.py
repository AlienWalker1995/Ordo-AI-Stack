"""Model Gateway — OpenAI-compatible proxy for Ollama, vLLM, and future providers."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from httpx import AsyncClient

app = FastAPI(title="Model Gateway", version="1.0.0")

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path != "/health":
        logger.info(">>> %s %s from=%s", request.method, request.url.path, request.client.host if request.client else "?")
    response = await call_next(request)
    if request.url.path != "/health":
        logger.info("<<< %s %s status=%s", request.method, request.url.path, response.status_code)
    return response

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
VLLM_URL = os.environ.get("VLLM_URL", "").rstrip("/")  # e.g. http://vllm:8000
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "ollama")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
MODEL_CACHE_TTL = float(os.environ.get("MODEL_CACHE_TTL_SEC", "60"))

# TTL model list cache: avoids hitting Ollama on every /v1/models call.
_model_cache: list = []
_model_cache_ts: float = 0.0


def _model_provider_and_id(name: str) -> tuple[str, str]:
    """Return (provider, model_id). Provider: ollama, vllm. Model ID is unprefixed."""
    if "/" in name:
        prefix, rest = name.split("/", 1)
        if prefix.lower() == "vllm" and VLLM_URL:
            return ("vllm", rest)
        return ("ollama", rest)
    return (DEFAULT_PROVIDER, name)


def _ollama_model_id(name: str) -> str:
    """Strip provider prefix if present (ollama/deepseek-r1:7b -> deepseek-r1:7b)."""
    _, model_id = _model_provider_and_id(name)
    return model_id


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
    if "openclaw" in o or ":18789" in o or ":18790" in o:
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
            async with AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{DASHBOARD_URL}/api/throughput/record",
                    json={
                        "model": model,
                        "output_tokens_per_sec": round(tps, 1),
                        "service": service or "unknown",
                    },
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
                    objects.append({
                        "id": f"ollama/{name}",
                        "object": "model",
                        "created": m.get("modified_at", 0) or 0,
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


@app.get("/health")
async def health():
    """Gateway health check. OK if at least one provider is reachable."""
    ok = False
    try:
        async with AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/version")
            ok = ok or r.status_code < 500
    except Exception:
        pass
    if VLLM_URL:
        try:
            async with AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{VLLM_URL}/health")
                ok = ok or r.status_code < 500
        except Exception:
            pass
    return {"ok": ok}


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


def _stream_chunk_openai(obj: dict) -> str:
    """Format OpenAI SSE chunk."""
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: dict[str, Any]):
    """Chat completion. Proxies to Ollama or vLLM based on model prefix."""
    model = body.get("model", "")
    provider, model_id = _model_provider_and_id(model)
    service = _service_from_headers(
        request.headers.get("Origin"),
        request.headers.get("X-Service-Name") or request.headers.get("X-Client-Id"),
    )
    req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    messages = body.get("messages", [])
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
    ollama_model = _ollama_model_id(model_id)
    ollama_body = {"model": ollama_model, "messages": messages, "stream": stream}

    if stream:
        async def generate():
            chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            first_sent = False
            last_eval_count = 0
            last_eval_duration = 0
            async with AsyncClient(timeout=3600.0) as client:
                async with client.stream(
                    "POST", f"{OLLAMA_URL}/api/chat", json=ollama_body
                ) as resp:
                    if resp.status_code >= 400:
                        err_body = await resp.aread()
                        logger.error("Ollama chat error status=%d body=%s", resp.status_code, err_body[:500])
                        return
                    async for line in resp.aiter_lines():
                        if not line or line == "data: [DONE]":
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("done"):
                                last_eval_count = data.get("eval_count", 0)
                                last_eval_duration = data.get("eval_duration", 0)
                            msg = data.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = "".join(
                                    p.get("text", "") if isinstance(p, dict) else str(p)
                                    for p in content
                                )
                            else:
                                content = str(content)
                            # Ollama sends per-token content, not cumulative
                            if content:
                                delta = {"content": content}
                                if not first_sent:
                                    delta["role"] = "assistant"
                                    first_sent = True
                                chunk = {
                                    "id": chunk_id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": model,
                                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                                }
                                yield _stream_chunk_openai(chunk)
                        except json.JSONDecodeError:
                            continue
            if last_eval_count and last_eval_duration:
                _record_throughput(model_id, last_eval_count, last_eval_duration, service)
            yield _stream_chunk_openai({
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"delta": {}, "finish_reason": "stop"}],
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
    return {
        "id": "chatcmpl-gateway",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": eval_count,
            "total_tokens": data.get("prompt_eval_count", 0) + eval_count,
        },
    }


# --- Legacy Completions (redirect to chat) ---


@app.post("/v1/completions")
async def completions_compat(request: Request, body: dict[str, Any]):
    """Legacy text completions — convert to chat format and proxy."""
    logger.warning(">>> /v1/completions called (legacy); converting to chat format. model=%s", body.get("model", ""))
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    chat_body = {
        "model": body.get("model", ""),
        "messages": [{"role": "user", "content": prompt}],
        "stream": body.get("stream", False),
    }
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        if k in body:
            chat_body[k] = body[k]
    return await chat_completions(request, chat_body)


# --- Responses API (OpenAI Responses format) ---


@app.post("/v1/responses")
async def responses_api(request: Request, body: dict[str, Any]):
    """OpenAI Responses API — convert to chat completions and proxy."""
    logger.info(">>> /v1/responses called; converting to chat format. model=%s stream=%s", body.get("model", ""), body.get("stream", False))

    messages: list[dict] = []
    instructions = body.get("instructions", "")
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

    inp = body.get("input", "")
    if isinstance(inp, str) and inp:
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                messages.append({"role": item.get("role", "user"), "content": _content_to_str(item.get("content"))})
            elif isinstance(item, str):
                messages.append({"role": "user", "content": item})

    stream = body.get("stream", False)
    chat_body: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
        "stream": stream,
    }
    for k in ("max_tokens", "max_output_tokens", "temperature", "top_p", "stop"):
        if k in body:
            chat_body[k] = body[k]

    chat_response = await chat_completions(request, chat_body)

    if stream or isinstance(chat_response, StreamingResponse):
        # OpenClaw with openai-responses expects Responses API streaming format,
        # not chat completions. Transform the stream.
        async def _to_responses_stream():
            resp_id = f"resp-{uuid.uuid4().hex[:12]}"
            item_id = f"msg-{uuid.uuid4().hex[:12]}"
            seq = 0
            model = body.get("model", "")
            # response.created
            yield _stream_chunk_openai({
                "type": "response.created",
                "response": {"id": resp_id, "created_at": int(time.time()), "model": model, "status": "in_progress"},
                "sequence_number": seq,
            })
            seq += 1
            # response.output_item.added (message with empty content)
            yield _stream_chunk_openai({
                "type": "response.output_item.added",
                "item": {"type": "message", "id": item_id, "role": "assistant", "content": []},
                "output_index": 0,
                "sequence_number": seq,
            })
            seq += 1
            # response.content_part.added (create output_text slot for streaming)
            yield _stream_chunk_openai({
                "type": "response.content_part.added",
                "content_index": 0,
                "item_id": item_id,
                "output_index": 0,
                "part": {"type": "output_text", "text": ""},
                "sequence_number": seq,
            })
            seq += 1
            # Consume chat stream, emit response.output_text.delta for each content
            buf = ""
            full_text = ""
            async for chunk in chat_response.body_iterator:
                buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    for line in block.strip().split("\n"):
                        line = line.strip()
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                data = json.loads(line[6:])
                                delta = (data.get("choices") or [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    full_text += content
                                    yield _stream_chunk_openai({
                                        "type": "response.output_text.delta",
                                        "delta": content,
                                        "item_id": item_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "sequence_number": seq,
                                    })
                                    seq += 1
                            except json.JSONDecodeError:
                                pass
            # response.output_text.done, response.content_part.done, response.output_item.done, response.done
            # Include full text so client doesn't overwrite with empty
            yield _stream_chunk_openai({
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "text": full_text,
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.content_part.done",
                "content_index": 0,
                "item_id": item_id,
                "output_index": 0,
                "part": {"type": "output_text", "text": full_text},
                "sequence_number": seq,
            })
            seq += 1
            yield _stream_chunk_openai({
                "type": "response.output_item.done",
                "item": {"type": "message", "id": item_id, "role": "assistant", "content": [{"type": "output_text", "text": full_text}]},
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

    resp_id = f"resp-{uuid.uuid4().hex[:12]}"
    choice = chat_response.get("choices", [{}])[0] if isinstance(chat_response, dict) else {}
    content = choice.get("message", {}).get("content", "")
    usage = chat_response.get("usage", {}) if isinstance(chat_response, dict) else {}

    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": body.get("model", ""),
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content}],
            }
        ],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "status": "completed",
    }


# --- Embeddings ---


@app.post("/v1/embeddings")
async def embeddings(body: dict[str, Any]):
    """Embeddings. Proxies to Ollama or vLLM based on model prefix."""
    model = body.get("model", "")
    provider, model_id = _model_provider_and_id(model)
    inp = body.get("input", "")

    if isinstance(inp, str):
        inp = [inp]
    if not inp:
        return {"object": "list", "data": [], "model": model}

    # vLLM: native OpenAI format
    if provider == "vllm" and VLLM_URL:
        async with AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{VLLM_URL}/v1/embeddings",
                json={**body, "model": model_id},
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    # Ollama: strip provider prefix (ollama/qwen2.5:7b -> qwen2.5:7b)
    ollama_model = _ollama_model_id(model_id)
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
