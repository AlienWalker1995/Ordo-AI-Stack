#!/usr/bin/env python3
"""Temporarily benchmark llama.cpp KV-cache profiles by editing .env, recreating llamacpp, and restoring .env."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROMPT = (
    "Explain in one paragraph why KV-cache quantization can reduce memory use but still hurt throughput "
    "when the backend falls off optimized GPU kernels."
)


@dataclass(frozen=True)
class KvProfile:
    name: str
    enabled: str
    type_k: str
    type_v: str


PROFILES = {
    "baseline": KvProfile("baseline", "0", "q4_0", "q4_0"),
    "q8_0": KvProfile("q8_0", "1", "q8_0", "q8_0"),
    "q4_0": KvProfile("q4_0", "1", "q4_0", "q4_0"),
    "q4_1": KvProfile("q4_1", "1", "q4_1", "q4_1"),
    "iq4_nl": KvProfile("iq4_nl", "1", "iq4_nl", "iq4_nl"),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profiles",
        default="baseline,q8_0,q4_0",
        help="Comma-separated profile names. Known values: " + ", ".join(PROFILES),
    )
    parser.add_argument("--repeats", type=int, default=2, help="Benchmarks per profile.")
    parser.add_argument("--model", default="", help="Model id for model-gateway /api/generate.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Benchmark prompt.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file to patch temporarily.",
    )
    parser.add_argument(
        "--gateway-url",
        default="http://localhost:11435",
        help="Base URL for model-gateway.",
    )
    parser.add_argument(
        "--llamacpp-health-url",
        default="http://localhost:11435/ready",
        help="Readiness URL to wait on after llamacpp recreate. Defaults to model-gateway /ready on the host.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=1800,
        help="How long to wait for llama.cpp to become healthy after recreate.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON output path. Defaults to a temp file.",
    )
    return parser.parse_args()


def _load_env_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _upsert_env(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            out.append(line)
            continue
        key, _, _value = line.partition("=")
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _read_simple_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _run_compose_recreate(repo_root: Path) -> None:
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--force-recreate", "llamacpp"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "docker compose recreate failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _wait_for_health(url: str, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for llama.cpp health at {url}. Last error: {last_error}")


def _post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def _benchmark_once(gateway_url: str, model: str, prompt: str) -> dict:
    return _post_json(
        gateway_url.rstrip("/") + "/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "max_tokens": 16,
            "temperature": 0,
        },
    )


def _summarize_run(run: dict) -> dict:
    eval_count = int(run.get("eval_count", 0) or 0)
    eval_duration_ns = int(run.get("eval_duration", 0) or 0)
    prompt_eval_count = int(run.get("prompt_eval_count", 0) or 0)
    prompt_eval_duration_ns = int(run.get("prompt_eval_duration", 0) or 0)
    output_tps = eval_count / (eval_duration_ns / 1e9) if eval_duration_ns else 0.0
    input_tps = prompt_eval_count / (prompt_eval_duration_ns / 1e9) if prompt_eval_duration_ns else 0.0
    return {
        "prompt_tokens": prompt_eval_count,
        "output_tokens": eval_count,
        "input_tokens_per_sec": round(input_tps, 2),
        "output_tokens_per_sec": round(output_tps, 2),
        "total_duration_ms": round((int(run.get("total_duration", 0) or 0)) / 1e6, 1),
    }


def main() -> int:
    args = _parse_args()
    env_path = Path(args.env_file).resolve()
    repo_root = env_path.parent
    requested_profiles = [name.strip() for name in args.profiles.split(",") if name.strip()]
    unknown = [name for name in requested_profiles if name not in PROFILES]
    if unknown:
        print(f"Unknown profiles: {', '.join(unknown)}", file=sys.stderr)
        return 2

    original_text = _load_env_text(env_path)
    env_values = _read_simple_env(env_path)
    model = args.model or env_values.get("DEFAULT_MODEL") or env_values.get("LLAMACPP_MODEL", "")
    if not model:
        print("No model id found. Set DEFAULT_MODEL or pass --model.", file=sys.stderr)
        return 2

    output_path = Path(args.output) if args.output else Path(
        tempfile.gettempdir(),
        f"llamacpp-kv-cache-benchmark-{int(time.time())}.json",
    )

    results: dict[str, object] = {
        "model": model,
        "profiles": [],
        "prompt": args.prompt,
        "gateway_url": args.gateway_url,
    }

    try:
        for name in requested_profiles:
            profile = PROFILES[name]
            patched = _upsert_env(
                original_text,
                {
                    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": profile.enabled,
                    "LLAMACPP_KV_CACHE_TYPE_K": profile.type_k,
                    "LLAMACPP_KV_CACHE_TYPE_V": profile.type_v,
                },
            )
            env_path.write_text(patched, encoding="utf-8")
            print(f"[profile:{name}] recreating llamacpp")
            _run_compose_recreate(repo_root)
            print(f"[profile:{name}] waiting for health")
            _wait_for_health(args.llamacpp_health_url, args.timeout_sec)

            runs = []
            for idx in range(args.repeats):
                print(f"[profile:{name}] benchmark {idx + 1}/{args.repeats}")
                raw = _benchmark_once(args.gateway_url, model, args.prompt)
                runs.append({"raw": raw, "summary": _summarize_run(raw)})

            output_tps = [run["summary"]["output_tokens_per_sec"] for run in runs]
            input_tps = [run["summary"]["input_tokens_per_sec"] for run in runs]
            profile_result = {
                "name": name,
                "kv_cache_quantization_enabled": profile.enabled == "1",
                "cache_type_k": profile.type_k,
                "cache_type_v": profile.type_v,
                "runs": runs,
                "avg_output_tokens_per_sec": round(sum(output_tps) / len(output_tps), 2),
                "avg_input_tokens_per_sec": round(sum(input_tps) / len(input_tps), 2),
            }
            cast_profiles = results["profiles"]
            assert isinstance(cast_profiles, list)
            cast_profiles.append(profile_result)
    finally:
        env_path.write_text(original_text, encoding="utf-8")
        try:
            print("[restore] recreating llamacpp with original .env")
            _run_compose_recreate(repo_root)
        except Exception as exc:  # noqa: BLE001
            results["restore_error"] = str(exc)

    output_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote benchmark results to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
