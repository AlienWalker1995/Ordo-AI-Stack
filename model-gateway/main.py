"""Model Gateway — OpenAI-compatible proxy for llama.cpp (llama-server), vLLM, and future providers."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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

LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://llamacpp:8080").rstrip("/")
LLAMACPP_EMBED_URL = os.environ.get("LLAMACPP_EMBED_URL", "http://llamacpp-embed:8080").rstrip("/")
VLLM_URL = os.environ.get("VLLM_URL", "").rstrip("/")  # e.g. http://vllm:8000
DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "llamacpp")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
THROUGHPUT_RECORD_TOKEN = os.environ.get("THROUGHPUT_RECORD_TOKEN", "").strip()
MODEL_CACHE_TTL = float(os.environ.get("MODEL_CACHE_TTL_SEC", "60"))
# When Claude Code sends a "claude-*" model name, remap it to this local model.
CLAUDE_CODE_LOCAL_MODEL = os.environ.get("CLAUDE_CODE_LOCAL_MODEL", "")
# If true, append synthetic claude-* ids to /v1/models (for clients that validate against the list).
# Default off — those fake Sonnet names pollute Open WebUI / OpenClaw "active models" sync.
CLAUDE_CODE_ADVERTISE_ALIASES = os.environ.get("CLAUDE_CODE_ADVERTISE_ALIASES", "").strip() == "1"
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("LLAMACPP_CTX_SIZE", "262144") or 262144)
# Hard input-token budget for OpenClaw requests. Prevents slow decode by trimming prompts before
# they reach llama.cpp, regardless of the model's full context window.
OPENCLAW_MAX_INPUT_TOKENS = int(os.environ.get("OPENCLAW_MAX_INPUT_TOKENS", "30720") or 30720)

# TTL model list cache: avoids hitting llama-server on every /v1/models call.
_model_cache: list = []
_model_cache_ts: float = 0.0

# Strip both <thinking> (Claude/OpenClaw format) and <think> (Qwen3/Gemma4 format).
_THINKING_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)

# Gemma 4 emits literal special tokens (<|"|>, <|'|>, etc.) in tool call arguments
# instead of the actual characters. Replace them so JSON parses correctly.


def _clean_gemma_special_tokens(text: str) -> str:
    """Replace Gemma special tokens (<|"|>, etc.) with their literal characters."""
    if "<|" not in text:
        return text
    text = text.replace('<|"|>', '"')
    text = text.replace("<|'|>", "'")
    text = text.replace("<|`|>", "`")
    text = text.replace("<|\\n|>", "\n")
    # Catch any remaining <|X|> single-char tokens
    text = re.sub(r"<\|(.)\|>", r"\1", text)
    return text


def _sanitize_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Clean Gemma special tokens from tool call arguments and ensure valid JSON."""
    for tc in tool_calls:
        func = tc.get("function") or {}
        args = func.get("arguments", "")
        if isinstance(args, str) and "<|" in args:
            cleaned = _clean_gemma_special_tokens(args)
            # Try to parse as JSON; if it still fails, wrap bare values
            try:
                json.loads(cleaned)
                func["arguments"] = cleaned
            except json.JSONDecodeError:
                # Might be missing outer braces or other issues
                if not cleaned.strip().startswith("{"):
                    cleaned = "{" + cleaned + "}"
                try:
                    json.loads(cleaned)
                    func["arguments"] = cleaned
                except json.JSONDecodeError:
                    func["arguments"] = cleaned  # pass through best-effort
    return tool_calls


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _stringify_message_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "value", "output"):
                    payload = item.get(key)
                    if isinstance(payload, str) and payload:
                        parts.append(payload)
                        break
        return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _message_token_cost(message: dict[str, Any]) -> int:
    cost = 8
    cost += _estimate_text_tokens(_stringify_message_payload(message.get("content")))
    if message.get("tool_calls"):
        cost += _estimate_text_tokens(_stringify_message_payload(message.get("tool_calls")))
    if message.get("tool_call_id"):
        cost += 4
    return cost


def _truncate_message_text(text: str, token_budget: int, keep_tail: bool = False) -> str:
    if token_budget <= 0:
        return ""
    max_chars = max(32, token_budget * 4)
    if len(text) <= max_chars:
        return text
    if keep_tail:
        return "... " + text[-(max_chars - 4):]
    return text[: max_chars - 4] + " ..."


def _truncate_message_to_budget(message: dict[str, Any], token_budget: int) -> dict[str, Any] | None:
    if token_budget <= 8:
        return None
    trimmed = dict(message)
    role = str(trimmed.get("role") or "")
    keep_tail = role not in {"system", "developer"}
    text_budget = max(1, token_budget - 8)

    if trimmed.get("content") is not None:
        text = _stringify_message_payload(trimmed.get("content"))
        trimmed["content"] = _truncate_message_text(text, text_budget, keep_tail=keep_tail)
    if trimmed.get("tool_calls"):
        trimmed["tool_calls"] = None
    return trimmed


def _trim_messages_to_budget(messages: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not messages:
        return (messages, {"before": 0, "after": 0, "dropped": 0})

    before = sum(_message_token_cost(m) for m in messages)
    if before <= budget:
        return (messages, {"before": before, "after": before, "dropped": 0})

    system_messages = [dict(m) for m in messages if m.get("role") in {"system", "developer"}]
    other_messages = [dict(m) for m in messages if m.get("role") not in {"system", "developer"}]

    kept_system: list[dict[str, Any]] = []
    used = 0
    system_budget = max(512, budget // 3)
    for msg in system_messages:
        remaining = max(0, system_budget - used)
        msg_cost = _message_token_cost(msg)
        if msg_cost > remaining:
            msg = _truncate_message_to_budget(msg, remaining)
            if not msg:
                continue
            msg_cost = _message_token_cost(msg)
        kept_system.append(msg)
        used += msg_cost

    remaining_budget = max(256, budget - used)
    kept_other_rev: list[dict[str, Any]] = []
    for msg in reversed(other_messages):
        msg_cost = _message_token_cost(msg)
        if msg_cost <= remaining_budget:
            kept_other_rev.append(msg)
            remaining_budget -= msg_cost
            continue
        trimmed = _truncate_message_to_budget(msg, remaining_budget)
        if trimmed:
            kept_other_rev.append(trimmed)
            remaining_budget = 0
        break

    trimmed_messages = kept_system + list(reversed(kept_other_rev))
    after = sum(_message_token_cost(m) for m in trimmed_messages)
    dropped = max(0, len(messages) - len(trimmed_messages))
    return (trimmed_messages, {"before": before, "after": after, "dropped": dropped})


_OPENCLAW_SLUG_HINT_RE = re.compile(
    r"\b("
    r"slug|title|conversation\s+title|session\s+title|chat\s+title|"
    r"short\s+title|concise\s+title|generate\s+a\s+slug|generate\s+a\s+title|"
    r"filename|label"
    r")\b",
    re.IGNORECASE,
)


def _responses_text_parts(inp: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(inp, str):
        if inp.strip():
            parts.append(inp.strip())
        return parts
    if not isinstance(inp, list):
        return parts
    for item in inp:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            if content.strip():
                parts.append(content.strip())
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
    return parts


def _slugify_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:64] or "chat"


def _title_from_text(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", text)
    title = " ".join(words[:8]).strip()
    return title[:80] or "Chat"


def _is_openclaw_slug_helper_request(service: str, body: ResponsesRequest) -> bool:
    if service != "openclaw":
        return False
    if body.tools:
        return False
    corpus = "\n".join([body.instructions or "", *_responses_text_parts(body.input)]).strip()
    if not corpus:
        return False
    if not _OPENCLAW_SLUG_HINT_RE.search(corpus):
        return False
    max_output = body.max_output_tokens or body.max_tokens or 0
    # Short-output + keyword match = very likely a slug/title helper, bypass eagerly.
    if isinstance(max_output, int) and 0 < max_output <= 64 and len(corpus) < 8000:
        return True
    # Longer-output but still a small metadata-only prompt with keyword match.
    return len(corpus) < 4000


def _synthetic_openclaw_slug_response(body: ResponsesRequest) -> dict[str, Any]:
    text_parts = _responses_text_parts(body.input)
    seed = text_parts[-1] if text_parts else (body.instructions or "chat")
    wants_slug = "slug" in (body.instructions or "").lower() or "slug" in seed.lower()
    rendered = _slugify_text(seed) if wants_slug else _title_from_text(seed)
    return {
        "id": f"resp-{uuid.uuid4().hex[:12]}",
        "object": "response",
        "created_at": int(time.time()),
        "model": body.model,
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": rendered}],
        }],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "status": "completed",
    }
# Only extract <result> when scoped inside an <attempt_completion> wrapper (Cline agent format).
_ATTEMPT_COMPLETION_RE = re.compile(
    r"<attempt_completion>\s*<result>\s*(.*?)\s*</result>\s*</attempt_completion>",
    re.IGNORECASE | re.DOTALL,
)


def _is_embedding_model_id(model_id: str) -> bool:
    lower = model_id.lower()
    return "embed" in lower or lower.startswith("text-embedding")


def _profiled_model_entries(model_id: str, created: int, owned_by: str) -> list[dict[str, Any]]:
    return [{
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
        "context_window": DEFAULT_CONTEXT_WINDOW,
    }]


def _is_likely_cline_request(request: Request) -> bool:
    """Best-effort detection for Cline's OpenAI-compatible client."""
    ua = (request.headers.get("user-agent") or "").lower()
    return (
        bool(request.headers.get("x-stainless-lang"))
        and "openai/js" in ua
        and not request.headers.get("X-Service-Name")
    )


def _sanitize_assistant_content(content: Any, preserve_agent_markup: bool = False) -> Any:
    """Normalize leaked agent-control wrappers into client-safe assistant text."""
    if not isinstance(content, str):
        return content

    text = content.strip()
    if not text:
        return content

    text = _THINKING_BLOCK_RE.sub("", text).strip()
    if preserve_agent_markup:
        return text

    attempt_match = _ATTEMPT_COMPLETION_RE.search(text)
    if attempt_match:
        return attempt_match.group(1).strip()

    return text


def _sanitize_openai_chat_response(
    data: dict[str, Any],
    preserve_agent_markup: bool = False,
) -> dict[str, Any]:
    """Return a stricter OpenAI chat payload for clients with fragile parsers."""
    if not isinstance(data, dict):
        return data

    choices = data.get("choices")
    if not isinstance(choices, list):
        return data

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        if "reasoning_content" in message:
            message.pop("reasoning_content", None)
        if "content" in message:
            message["content"] = _sanitize_assistant_content(
                message["content"],
                preserve_agent_markup=preserve_agent_markup,
            )
        if isinstance(message.get("tool_calls"), list):
            message["tool_calls"] = _sanitize_tool_calls(message["tool_calls"])

    return data


def _sanitize_openai_stream_event(data: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize streamed chat chunks to a conservative OpenAI-compatible subset."""
    if not isinstance(data, dict):
        return data

    choices = data.get("choices")
    if not isinstance(choices, list):
        return data

    sanitized_choices: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        sanitized_choice = dict(choice)

        delta = sanitized_choice.get("delta")
        if isinstance(delta, dict):
            delta = dict(delta)
            delta.pop("reasoning_content", None)
            if isinstance(delta.get("tool_calls"), list):
                delta["tool_calls"] = _sanitize_tool_calls(delta["tool_calls"])
            if not delta:
                sanitized_choice.pop("delta", None)
            else:
                sanitized_choice["delta"] = delta

        message = sanitized_choice.get("message")
        if isinstance(message, dict):
            message = dict(message)
            message.pop("reasoning_content", None)
            if "content" in message:
                message["content"] = _sanitize_assistant_content(message["content"])
            if isinstance(message.get("tool_calls"), list):
                message["tool_calls"] = _sanitize_tool_calls(message["tool_calls"])
            sanitized_choice["message"] = message

        has_payload = any(
            sanitized_choice.get(key) not in (None, "", [], {})
            for key in ("delta", "message", "finish_reason")
        )
        if has_payload:
            sanitized_choices.append(sanitized_choice)

    if not sanitized_choices and not data.get("usage"):
        return None

    data["choices"] = sanitized_choices
    return data


def _stream_chunk_bytes(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _model_provider_and_id(name: str) -> tuple[str, str]:
    """Return (provider, model_id). Provider: llamacpp, vllm.
    Explicit prefixes: vllm/, llamacpp/, ollama/ (legacy), gateway/ (OpenClaw provider id)."""
    if name.startswith("vllm/") and VLLM_URL:
        return ("vllm", name[5:])
    if name.startswith("llamacpp/"):
        return ("llamacpp", name[9:])
    if name.startswith("ollama/"):
        return ("llamacpp", name[7:])
    # OpenClaw uses agents.defaults.model.primary like "gateway/<modelId>" while /v1/models lists bare ids.
    if name.startswith("gateway/"):
        return (DEFAULT_PROVIDER, name[8:])
    return (DEFAULT_PROVIDER, name)


def _inference_model_id(name: str) -> str:
    """Strip provider prefix; result is the model id sent to llama-server / vLLM."""
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
    if "openclaw" in o or ":6666" in o or ":6667" in o or ":18789" in o or ":18790" in o:
        return "openclaw"
    # Fallback: host:port
    try:
        return origin.replace("http://", "").replace("https://", "").split("/")[0][:64]
    except Exception:
        return "unknown"


def _record_throughput(
    model: str, eval_count: int, eval_duration_ns: int, service: str = "", ttft_ms: float = 0.0
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
                        "ttft_ms": round(ttft_ms, 1) if ttft_ms > 0 else 0.0,
                    },
                    headers=headers if headers else None,
                )
        except Exception:
            pass

    asyncio.create_task(_post())


# --- Models ---


@app.get("/v1/models")
async def list_models():
    """List models in OpenAI format. Aggregates from llama-server and vLLM (when configured).
    Results are cached for MODEL_CACHE_TTL_SEC seconds.
    """
    global _model_cache, _model_cache_ts

    # Serve from cache if still fresh
    if MODEL_CACHE_TTL > 0 and _model_cache and (time.monotonic() - _model_cache_ts) < MODEL_CACHE_TTL:
        return {"object": "list", "data": _model_cache}

    objects = []

    # llama.cpp server (OpenAI /v1/models)
    async with AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(f"{LLAMACPP_URL}/v1/models")
            if r.status_code < 500:
                data = r.json()
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        objects.extend(_profiled_model_entries(mid, m.get("created", 0) or 0, "llamacpp"))
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
                            resolved_id = f"vllm/{mid}" if "/" not in mid else mid
                            objects.extend(_profiled_model_entries(resolved_id, m.get("created", 0) or 0, "vllm"))
        except Exception:
            pass

    # Optional: advertise fixed claude-* ids in /v1/models. Remapping in chat still works
    # without this (see _resolve_claude_model). Opt-in — do not sync fake models to OpenClaw by default.
    if CLAUDE_CODE_LOCAL_MODEL and CLAUDE_CODE_ADVERTISE_ALIASES and objects:
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


# --- Ollama-shaped compatibility (n8n / legacy clients → llama-server) ---


async def _ollama_tags_from_llamacpp() -> dict:
    async with AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{LLAMACPP_URL}/v1/models")
        r.raise_for_status()
        data = r.json()
        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if mid:
                models.append({"name": mid, "modified_at": m.get("created", 0) or 0})
        return {"models": models}


@app.get("/api/tags")
async def ollama_tags():
    """Ollama-shaped tag list from llama-server /v1/models."""
    return await _ollama_tags_from_llamacpp()


@app.get("/api/ps")
async def ollama_ps():
    """Ollama-shaped loaded models (from /v1/models)."""
    data = await _ollama_tags_from_llamacpp()
    return {"models": [{"name": m["name"], "size": 0, "digest": "", "details": {}} for m in data.get("models", [])]}


@app.delete("/api/delete")
async def ollama_delete(request: Request):
    """No-op success: Ollama returns 200 with empty body. GGUF files are not removed from disk."""
    # Clients (OpenClaw, Ollama SDK) call this to unload a model; llama-server uses a fixed GGUF on disk.
    try:
        await request.body()
    except Exception:
        pass
    return Response(status_code=200)


@app.post("/api/pull")
async def ollama_pull(request: Request):
    """Ollama pull not supported — use profile models + GGUF puller."""
    return JSONResponse(
        status_code=501,
        content={"error": "run: docker compose --profile models run --rm gguf-puller (GGUF_MODELS=...)"},
    )


@app.post("/api/generate")
async def ollama_generate(request: Request):
    """Map legacy generate to OpenAI /v1/completions on llama-server.

    Non-stream responses are shaped like Ollama /api/generate so the dashboard
    throughput benchmark (eval_count, eval_duration ns, etc.) keeps working.
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    stream = bool(body.get("stream", False))
    fwd = {
        "model": body.get("model", ""),
        "prompt": prompt,
        "stream": stream,
    }
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        if k in body and body[k] is not None:
            fwd[k] = body[k]
    if stream:
        async def _stream_completions():
            async with AsyncClient(timeout=600.0) as client:
                async with client.stream(
                    "POST",
                    f"{LLAMACPP_URL}/v1/completions",
                    json=fwd,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(_stream_completions(), media_type="text/event-stream")

    t0 = time.perf_counter()
    async with AsyncClient(timeout=600.0) as client:
        r = await client.post(f"{LLAMACPP_URL}/v1/completions", json=fwd, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
    elapsed_ns = max(int((time.perf_counter() - t0) * 1e9), 1)
    choice0 = (data.get("choices") or [{}])[0]
    text = choice0.get("text", "") or ""
    usage = data.get("usage") or {}
    comp = int(usage.get("completion_tokens") or 0)
    pr = int(usage.get("prompt_tokens") or 0)
    # Split wall time: weight toward completion tokens for sensible tok/s when usage is present
    if comp > 0 and pr >= 0:
        tot_tok = comp + pr
        eval_ns = max(int(elapsed_ns * comp / tot_tok), 1) if tot_tok else elapsed_ns
        prompt_ns = max(elapsed_ns - eval_ns, 1)
    else:
        eval_ns = elapsed_ns
        prompt_ns = max(elapsed_ns // 4, 1)
    return {
        "model": body.get("model", ""),
        "response": text,
        "done": True,
        "eval_count": comp,
        "eval_duration": eval_ns,
        "prompt_eval_count": pr,
        "prompt_eval_duration": prompt_ns,
        "load_duration": 0,
        "total_duration": elapsed_ns,
    }


@app.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama /api/chat → OpenAI chat completions on llama-server (non-stream); stream passes SSE→NDJSON-ish."""
    body = await request.json()
    model = body.get("model", "")
    messages = body.get("messages") or []
    stream = bool(body.get("stream", False))
    chat_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if body.get("tools"):
        chat_body["tools"] = body["tools"]
    if "tool_choice" in body:
        chat_body["tool_choice"] = body["tool_choice"]
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        if k in body and body[k] is not None:
            chat_body[k] = body[k]

    if not stream:
        async with AsyncClient(timeout=600.0) as client:
            r = await client.post(
                f"{LLAMACPP_URL}/v1/chat/completions",
                json=chat_body,
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content", "") or ""
        usage = data.get("usage") or {}
        ollama_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if msg.get("tool_calls"):
            ollama_tcs = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (json.JSONDecodeError, ValueError):
                    args = {}
                ollama_tcs.append({
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args},
                })
            ollama_msg["tool_calls"] = ollama_tcs
        return {
            "model": model,
            "created_at": data.get("created", ""),
            "message": ollama_msg,
            "done": True,
            "prompt_eval_count": usage.get("prompt_tokens", 0),
            "eval_count": usage.get("completion_tokens", 0),
            "eval_duration": int(1e9),
        }

    async def ollama_stream():
        async with AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST",
                f"{LLAMACPP_URL}/v1/chat/completions",
                json=chat_body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    yield json.dumps({"error": err, "done": True}) + "\n"
                    return
                buf = b""
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    while b"\n\n" in buf:
                        line, buf = buf.split(b"\n\n", 1)
                        for part in line.split(b"\n"):
                            if not part.startswith(b"data: "):
                                continue
                            payload = part[6:].strip()
                            if payload == b"[DONE]":
                                yield json.dumps({"done": True, "eval_count": 0, "eval_duration": 0}) + "\n"
                                return
                            try:
                                d = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            c0 = (d.get("choices") or [{}])[0]
                            delta = c0.get("delta", {})
                            text = delta.get("content") or ""
                            if text:
                                yield json.dumps({
                                    "model": model,
                                    "message": {"role": "assistant", "content": text},
                                    "done": False,
                                }) + "\n"
                            if c0.get("finish_reason"):
                                yield json.dumps({"done": True, "eval_count": 0, "eval_duration": 0}) + "\n"
                                return

    return StreamingResponse(
        ollama_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/embed")
async def ollama_embed(request: Request):
    """Ollama /api/embed → OpenAI /v1/embeddings on embed server."""
    body = await request.json()
    inp = body.get("input", "")
    if isinstance(inp, str):
        inp = [inp]
    fwd = {"model": body.get("model", ""), "input": inp}
    async with AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{LLAMACPP_EMBED_URL}/v1/embeddings",
            json=fwd,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    vecs = []
    for i, item in enumerate(data.get("data", [])):
        emb = item.get("embedding", [])
        vecs.append(emb)
    return {"model": body.get("model", ""), "embeddings": vecs}


@app.get("/")
async def root():
    return "llama.cpp (via model-gateway)"


@app.get("/api/version")
async def api_version():
    return {"version": "0.0.0-llamacpp"}


@app.post("/api/show")
async def api_show(request: Request):
    try:
        raw = await request.json()
        name = raw.get("name", "")
        async with AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{LLAMACPP_URL}/v1/models")
            if r.status_code < 500:
                for m in r.json().get("data", []):
                    if m.get("id") == name:
                        return {"modelfile": "", "parameters": "", "template": "", "details": {"family": "gguf", "name": name}}
    except Exception:
        pass
    return {"error": "model not found"}


async def _probe_llamacpp_l1(client: AsyncClient) -> dict:
    t0 = time.monotonic()
    try:
        r = await client.get(f"{LLAMACPP_URL}/health")
        ms = round((time.monotonic() - t0) * 1000, 1)
        ok = r.status_code < 500
        try:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            body = {}
        return {
            "ok": ok and (not body or body.get("status") in (None, "ok")),
            "status_code": r.status_code,
            "latency_ms": ms,
            "status": body.get("status"),
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
        providers["llamacpp"] = await _probe_llamacpp_l1(client)
        ok = ok or providers["llamacpp"].get("ok") is True
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
    """L2 readiness: llama-server (or vLLM) up and at least one model listed."""
    providers: dict[str, Any] = {}
    any_l1 = False
    model_count = 0

    async with AsyncClient(timeout=5.0) as client:
        providers["llamacpp"] = await _probe_llamacpp_l1(client)
        any_l1 = any_l1 or providers["llamacpp"].get("ok") is True
        try:
            r = await client.get(f"{LLAMACPP_URL}/v1/models")
            if r.status_code < 500:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                n = len(data.get("data") or [])
                model_count += n
                providers["llamacpp"]["models_ok"] = True
                providers["llamacpp"]["model_count"] = n
            else:
                providers["llamacpp"]["models_ok"] = False
        except Exception as e:
            providers["llamacpp"]["models_ok"] = False
            providers["llamacpp"]["models_error"] = str(e)

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


def _normalize_messages_openai(msgs: list[dict]) -> list[dict]:
    """Normalize messages to OpenAI chat format for llama-server (string tool arguments)."""
    out: list[dict[str, Any]] = []
    for m in msgs:
        role = m.get("role", "")
        content = m.get("content")

        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    if p.get("type") in ("text", "output_text") and "text" in p:
                        parts.append(str(p["text"]))
                    elif p.get("type") == "tool_result":
                        inner = p.get("content", "")
                        if isinstance(inner, list):
                            inner = "".join(
                                x.get("text", "") if isinstance(x, dict) else str(x)
                                for x in inner
                            )
                        parts.append(str(inner))
                    else:
                        for k in ("text", "content", "value"):
                            if k in p:
                                parts.append(str(p[k]))
                                break
                elif isinstance(p, str):
                    parts.append(p)
            content = "\n".join(parts)

        if content is None:
            content = ""

        msg_out: dict[str, Any] = {"role": role, "content": content}
        if "tool_call_id" in m:
            msg_out["tool_call_id"] = m["tool_call_id"]

        raw_tcs = m.get("tool_calls")
        if raw_tcs:
            normalized_tcs = []
            for tc in raw_tcs:
                func = (tc.get("function") or {})
                args = func.get("arguments", {})
                if isinstance(args, dict):
                    args_str = json.dumps(args)
                elif args is None:
                    args_str = "{}"
                else:
                    args_str = str(args)
                normalized_tcs.append({
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {"name": func.get("name", ""), "arguments": args_str},
                })
            msg_out["tool_calls"] = normalized_tcs

        out.append(msg_out)
    return out


def _record_usage_throughput(
    model_id: str, usage: dict | None, service: str, elapsed_ns: int, ttft_ms: float = 0.0
) -> None:
    """Record real tokens/sec using wall time for the request/stream (not a fake 1s duration)."""
    if not usage:
        return
    ct = int(usage.get("completion_tokens") or 0)
    if ct <= 0:
        return
    dur = max(int(elapsed_ns), 1)
    _record_throughput(model_id, ct, dur, service, ttft_ms=ttft_ms)


def _parse_qwen_xml_tool_calls(text: str) -> list[dict]:
    """Parse Qwen3 XML-format tool calls from llamacpp error response text.

    Qwen3 emits tool calls as:
        <tool_call>
        <function=name>
        <parameter=key>value</parameter>
        </function>
        </tool_call>

    Llamacpp's grammar validator rejects this (expects JSON format) and returns
    a 500 error whose body contains the model's raw output. We recover the tool
    calls from there and return them in OpenAI function-call format so that
    OpenClaw can execute them normally.
    """
    calls: list[dict] = []
    for block in re.finditer(r'<tool_call>(.*?)(?:</tool_call>|$)', text, re.DOTALL):
        body = block.group(1)
        fn_match = re.search(r'<function=([^\n>]+)>(.*?)(?:</function>|$)', body, re.DOTALL)
        if not fn_match:
            continue
        fn_name = fn_match.group(1).strip()
        fn_body = fn_match.group(2)
        params: dict = {}
        for pm in re.finditer(r'<parameter=([^\n>]+)>(.*?)</parameter>', fn_body, re.DOTALL):
            params[pm.group(1).strip()] = pm.group(2).strip()
        calls.append({
            "index": len(calls),
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": fn_name, "arguments": json.dumps(params)},
        })
    return calls


async def _restream_llamacpp_chat_from_nonstream(
    model_id: str,
    service: str,
    fwd: dict[str, Any],
    req_id: str,
    preserve_agent_markup: bool = False,
):
    """Emit a minimal, sanitized OpenAI SSE stream from one non-stream backend call."""
    nonstream_fwd = dict(fwd)
    nonstream_fwd["stream"] = False
    nonstream_fwd.pop("stream_options", None)

    t0 = time.perf_counter()
    async with AsyncClient(timeout=600.0) as client:
        r = await client.post(
            f"{LLAMACPP_URL}/v1/chat/completions",
            json=nonstream_fwd,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            err_full = r.text
            logger.error("llama-server chat non-stream error status=%s body=%s", r.status_code, err_full[:500])
            # Qwen3 grammar failure recovery: llamacpp rejects the model's XML tool calls
            # via grammar validation. The model output is embedded in the error body — extract it.
            xml_calls = _parse_qwen_xml_tool_calls(err_full)
            if xml_calls:
                chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())
                yield _stream_chunk_bytes({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                })
                yield _stream_chunk_bytes({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"tool_calls": xml_calls}, "finish_reason": None}],
                })
                yield _stream_chunk_bytes({
                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                })
                yield b"data: [DONE]\n\n"
                return
            yield _stream_chunk_bytes({"error": {"message": err_full[:500]}})
            yield b"data: [DONE]\n\n"
            return
        data = _sanitize_openai_chat_response(
            r.json(),
            preserve_agent_markup=preserve_agent_markup,
        )

    elapsed_ns = max(int((time.perf_counter() - t0) * 1e9), 1)
    # ttft_ms is not measurable from a non-stream backend call; omit rather than misreport.
    usage = data.get("usage")
    if usage:
        _record_usage_throughput(model_id, usage, service, elapsed_ns)

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = _sanitize_tool_calls(message.get("tool_calls") or [])
    model_name = data.get("model", model_id)
    created = data.get("created", int(time.time()))
    chunk_id = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")

    yield _stream_chunk_bytes({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })

    if tool_calls:
        yield _stream_chunk_bytes({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": None}],
        })
    elif content:
        yield _stream_chunk_bytes({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        })

    final_chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop"),
        }],
    }
    if usage:
        final_chunk["usage"] = usage
    yield _stream_chunk_bytes(final_chunk)
    yield b"data: [DONE]\n\n"


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
    """Chat completion. Proxies to llama-server or vLLM based on model prefix."""
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
    preserve_agent_markup = _is_likely_cline_request(request)
    req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    messages = [
        {**m, "role": "system"} if m.get("role") == "developer" else m
        for m in body.get("messages", [])
    ]
    if service == "openclaw" and OPENCLAW_MAX_INPUT_TOKENS > 0:
        trimmed_messages, trim_stats = _trim_messages_to_budget(messages, OPENCLAW_MAX_INPUT_TOKENS)
        if trim_stats["after"] < trim_stats["before"]:
            logger.info(
                "trimmed openclaw prompt cid=%s before=%s after=%s dropped=%s budget=%s",
                req_id,
                trim_stats["before"],
                trim_stats["after"],
                trim_stats["dropped"],
                OPENCLAW_MAX_INPUT_TOKENS,
            )
        messages = trimmed_messages
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

    backend_model = _inference_model_id(model)
    fwd: dict[str, Any] = {
        "model": backend_model,
        "messages": _normalize_messages_openai(messages),
        "stream": stream,
    }
    if body.get("tools"):
        fwd["tools"] = body["tools"]
    if "tool_choice" in body:
        fwd["tool_choice"] = body["tool_choice"]
    for k in ("max_tokens", "temperature", "top_p", "stop"):
        if k in body and body[k] is not None:
            fwd[k] = body[k]

    if stream:
        so = dict(body.get("stream_options") or {})
        so.setdefault("include_usage", True)
        fwd["stream_options"] = so
        return StreamingResponse(
            _restream_llamacpp_chat_from_nonstream(
                model_id,
                service,
                fwd,
                req_id,
                preserve_agent_markup=preserve_agent_markup,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": req_id},
        )

    t0 = time.perf_counter()
    async with AsyncClient(timeout=600.0) as client:
        r = await client.post(
            f"{LLAMACPP_URL}/v1/chat/completions",
            json=fwd,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code >= 400:
            err_full = r.text
            logger.error("llama-server chat error status=%s body=%s", r.status_code, err_full[:500])
            # Qwen3 grammar failure recovery: extract XML tool calls from llamacpp error body.
            xml_calls = _parse_qwen_xml_tool_calls(err_full)
            if xml_calls:
                chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                data = {
                    "id": chunk_id, "object": "chat.completion", "created": int(time.time()),
                    "model": model_id,
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{k: v for k, v in tc.items() if k != "index"} for tc in xml_calls],
                    }, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                data["_request_id"] = req_id
                return data
            r.raise_for_status()
        data = r.json()
    data = _sanitize_openai_chat_response(
        data,
        preserve_agent_markup=preserve_agent_markup,
    )
    elapsed_ns = max(int((time.perf_counter() - t0) * 1e9), 1)
    _record_usage_throughput(model_id, data.get("usage"), service, elapsed_ns)
    data["_request_id"] = req_id
    return data


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
    service = _service_from_headers(
        request.headers.get("Origin"),
        request.headers.get("X-Service-Name") or request.headers.get("X-Client-Id"),
    )
    if _is_openclaw_slug_helper_request(service, body):
        logger.info(
            "short-circuiting openclaw slug/title helper cid=%s",
            getattr(request.state, "correlation_id", "?"),
        )
        return _synthetic_openclaw_slug_response(body)

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

            buf = ""
            full_text = ""
            msg_item_opened = False
            # tool_calls_acc: index → {id, name, arguments}
            tool_calls_acc: dict[int, dict] = {}
            tool_call_item_ids: dict[int, str] = {}

            def _ensure_msg_item_open() -> None:
                nonlocal msg_item_opened, seq
                if not msg_item_opened:
                    msg_item_opened = True
                    # msg_item_id already defined in outer scope

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

                                # Text content — open message item on first text token
                                content = delta.get("content", "")
                                if content:
                                    if not msg_item_opened:
                                        msg_item_opened = True
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
                                            "output_index": tc_idx,
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
                                            "output_index": tc_idx,
                                            "delta": args_delta,
                                            "sequence_number": seq,
                                        })
                                        seq += 1
                            except json.JSONDecodeError:
                                pass
            except Exception as exc:
                logger.error("Response stream error: %s", exc)

            # Build final output items list for response.done
            final_output: list[dict] = []

            # Finalize tool calls (emitted at their natural index, 0-based)
            for tc_idx, tc in sorted(tool_calls_acc.items()):
                tc["arguments"] = _clean_gemma_special_tokens(tc["arguments"])
                fc_item_id = tool_call_item_ids[tc_idx]
                fc_item = {
                    "type": "function_call",
                    "id": fc_item_id,
                    "call_id": tc["id"],
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                }
                final_output.append(fc_item)
                yield _stream_chunk_openai({
                    "type": "response.function_call_arguments.done",
                    "item_id": fc_item_id,
                    "output_index": tc_idx,
                    "arguments": tc["arguments"],
                    "sequence_number": seq,
                })
                seq += 1
                yield _stream_chunk_openai({
                    "type": "response.output_item.done",
                    "item": fc_item,
                    "output_index": tc_idx,
                    "sequence_number": seq,
                })
                seq += 1

            # Finalize text message item only if it was opened (model produced text)
            if msg_item_opened:
                msg_item = {"type": "message", "id": msg_item_id, "role": "assistant",
                            "content": [{"type": "output_text", "text": full_text}]}
                final_output.append(msg_item)
                yield _stream_chunk_openai({
                    "type": "response.output_text.done",
                    "item_id": msg_item_id,
                    "output_index": len(tool_calls_acc),
                    "content_index": 0,
                    "text": full_text,
                    "sequence_number": seq,
                })
                seq += 1
                yield _stream_chunk_openai({
                    "type": "response.content_part.done",
                    "content_index": 0,
                    "item_id": msg_item_id,
                    "output_index": len(tool_calls_acc),
                    "part": {"type": "output_text", "text": full_text},
                    "sequence_number": seq,
                })
                seq += 1
                yield _stream_chunk_openai({
                    "type": "response.output_item.done",
                    "item": msg_item,
                    "output_index": len(tool_calls_acc),
                    "sequence_number": seq,
                })
                seq += 1
            elif not tool_calls_acc:
                # No text and no tool calls — emit minimal empty message item
                msg_item = {"type": "message", "id": msg_item_id, "role": "assistant",
                            "content": [{"type": "output_text", "text": ""}]}
                final_output.append(msg_item)

            yield _stream_chunk_openai({
                "type": "response.done",
                "response": {
                    "id": resp_id,
                    "created_at": int(time.time()),
                    "model": model,
                    "status": "completed",
                    "output": final_output,
                },
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


def _is_anthropic_web_search_tool(t: dict) -> bool:
    ttype = t.get("type")
    return isinstance(ttype, str) and ttype.startswith("web_search_")


def _anthropic_last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
            return "\n".join(parts).strip()
    return ""


def _inject_anthropic_system(system: Any, text: str) -> Any:
    if system is None:
        return text
    if isinstance(system, str):
        return (system.rstrip() + "\n\n" + text) if system.strip() else text
    if isinstance(system, list):
        return list(system) + [{"type": "text", "text": text}]
    return text


async def _tavily_search_snippets(query: str, api_key: str) -> str:
    if not query or not api_key:
        return ""
    try:
        async with AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query[:2000],
                    "search_depth": "basic",
                    "max_results": 8,
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("Tavily search failed: %s", exc)
        return ""
    results = data.get("results") or []
    lines: list[str] = []
    for i, res in enumerate(results, 1):
        if not isinstance(res, dict):
            continue
        title = str(res.get("title", ""))
        url = str(res.get("url", ""))
        snippet = str(res.get("content", ""))[:600]
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(lines)


async def _bridge_claude_code_web_search_tools(raw: dict) -> dict:
    """Strip Anthropic web_search_* server tools (Ollama cannot run them) and inject Tavily text when configured."""
    tools_in = raw.get("tools") or []
    if not isinstance(tools_in, list):
        return raw
    web_tools = [t for t in tools_in if isinstance(t, dict) and _is_anthropic_web_search_tool(t)]
    if not web_tools:
        return raw

    out = dict(raw)
    remaining = [t for t in tools_in if isinstance(t, dict) and not _is_anthropic_web_search_tool(t)]
    if remaining:
        out["tools"] = remaining
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "Anthropic web_search tool(s) requested but TAVILY_API_KEY is unset; stripped tools without web context"
        )
        return out

    q = _anthropic_last_user_text(out.get("messages") or [])
    if not q:
        return out

    snippets = await _tavily_search_snippets(q, api_key)
    if not snippets:
        return out

    inject = (
        "Web search results retrieved for the latest user message (use for grounding; cite URLs when you use facts):\n\n"
        + snippets
    )
    out["system"] = _inject_anthropic_system(out.get("system"), inject)
    return out


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
    raw = await _bridge_claude_code_web_search_tools(raw)
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
                tool_calls_acc[tc_idx]["arguments"] = _clean_gemma_special_tokens(
                    tool_calls_acc[tc_idx]["arguments"]
                )
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
    """Embeddings. Proxies to llama-server (embed) or vLLM based on model prefix."""
    model = body.model
    provider, model_id = _model_provider_and_id(model)
    inp = body.input

    if isinstance(inp, str):
        inp = [inp]
    if not inp:
        return {"object": "list", "data": [], "model": model}

    if provider == "vllm" and VLLM_URL:
        async with AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{VLLM_URL}/v1/embeddings",
                json={**body.model_dump(exclude_none=True), "model": model_id},
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            return r.json()

    embed_model = _inference_model_id(model)
    payload = {"model": embed_model, "input": inp}
    async with AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{LLAMACPP_EMBED_URL}/v1/embeddings",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()
