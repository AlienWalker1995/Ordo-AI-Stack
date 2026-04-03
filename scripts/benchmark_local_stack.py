#!/usr/bin/env python3
"""Lightweight benchmark helper for model-gateway plus dashboard performance summary."""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        elapsed = time.perf_counter() - started
        return resp.status, json.loads(resp.read().decode("utf-8")), elapsed


def _pick_model(models: list[dict[str, Any]], prefer_chat: bool) -> str:
    ids = [str(item.get("id", "")) for item in models if item.get("id")]
    if prefer_chat:
        for model_id in ids:
            if model_id.endswith(":chat"):
                return model_id
    for model_id in ids:
        if ":chat" not in model_id and "embed" not in model_id.lower():
            return model_id
    if ids:
        return ids[0]
    raise RuntimeError("No models available from model-gateway /v1/models")


def _one_request(base_url: str, model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": max_tokens,
    }
    status, data, elapsed = _post_json(f"{base_url.rstrip('/')}/v1/chat/completions", body)
    usage = data.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens") or 0)
    tps = (completion_tokens / elapsed) if elapsed > 0 and completion_tokens > 0 else 0.0
    return {
        "status": status,
        "elapsed_sec": elapsed,
        "completion_tokens": completion_tokens,
        "tokens_per_sec": tps,
        "model": data.get("model", model),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default="http://localhost:11435")
    parser.add_argument("--dashboard-url", default="http://localhost:8080")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt", default="Summarize the performance tradeoffs of long-context local inference in 3 bullets.")
    parser.add_argument("--requests", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--prefer-chat-profile", action="store_true")
    args = parser.parse_args()

    models_payload = _get_json(f"{args.gateway_url.rstrip('/')}/v1/models")
    models = models_payload.get("data", []) if isinstance(models_payload, dict) else []
    model = args.model or _pick_model(models, prefer_chat=args.prefer_chat_profile)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [
            pool.submit(_one_request, args.gateway_url, model, args.prompt, args.max_tokens)
            for _ in range(max(1, args.requests))
        ]
        for future in as_completed(futures):
            results.append(future.result())

    latencies = [item["elapsed_sec"] for item in results]
    tps_values = [item["tokens_per_sec"] for item in results if item["tokens_per_sec"] > 0]
    summary = {
        "model": model,
        "request_count": len(results),
        "concurrency": max(1, args.concurrency),
        "latency_sec": {
            "min": round(min(latencies), 3),
            "p50": round(statistics.median(latencies), 3),
            "max": round(max(latencies), 3),
        },
        "tokens_per_sec": {
            "avg": round(sum(tps_values) / len(tps_values), 2) if tps_values else 0.0,
            "max": round(max(tps_values), 2) if tps_values else 0.0,
        },
        "results": results,
    }

    try:
        summary["dashboard_performance"] = _get_json(f"{args.dashboard_url.rstrip('/')}/api/performance/summary")
    except urllib.error.URLError as exc:
        summary["dashboard_performance_error"] = str(exc)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
