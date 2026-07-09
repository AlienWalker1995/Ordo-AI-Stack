"""Native (non-Docker) path — Docker-primary, native-best-effort.

The same rendered config that produces the compose stack also produces a native launch plan: the
exact `llama-server` command line (identical knobs to the container), plus honest notes about the
pieces native mode does NOT manage for you. This proves the declarative source is deployment-mode
agnostic — Docker or bare process, one source, no divergence.

Best-effort by design: llama.cpp runs natively cleanly; the gateways/agent are containers-first, so
native mode surfaces them as manual steps rather than pretending to orchestrate them.
"""
from __future__ import annotations

import dataclasses
import shlex
from pathlib import Path


def llama_server_argv(env: dict[str, str], models_dir: str = "./models",
                      host: str = "127.0.0.1", port: int = 8080) -> list[str]:
    """Build the llama-server argv from the rendered LLAMACPP_* env — the same values the
    container consumes, so native and Docker serve byte-identical model config."""
    def g(k: str, default: str = "") -> str:
        return str(env.get(k, default)).strip()

    argv: list[str] = ["llama-server", "--host", host, "--port", str(port)]
    model = g("LLAMACPP_MODEL")
    if model:
        # env carries the filename; native joins it under the models dir (the container mounts it)
        argv += ["--model", str(Path(models_dir) / model)]
    if g("LLAMACPP_CTX_SIZE"):
        argv += ["--ctx-size", g("LLAMACPP_CTX_SIZE")]
    if g("LLAMACPP_GPU_LAYERS"):
        argv += ["--n-gpu-layers", g("LLAMACPP_GPU_LAYERS")]
    if g("LLAMACPP_PARALLEL"):
        argv += ["--parallel", g("LLAMACPP_PARALLEL")]
    fa = g("LLAMACPP_FLASH_ATTN")
    if fa:
        argv += ["--flash-attn", fa]
    if g("LLAMACPP_KV_CACHE_TYPE_K"):
        argv += ["--cache-type-k", g("LLAMACPP_KV_CACHE_TYPE_K")]
    if g("LLAMACPP_KV_CACHE_TYPE_V"):
        argv += ["--cache-type-v", g("LLAMACPP_KV_CACHE_TYPE_V")]
    rope = g("LLAMACPP_ROPE_SCALING")
    if rope and rope != "none":
        argv += ["--rope-scaling", rope]
        if g("LLAMACPP_ROPE_SCALE"):
            argv += ["--rope-scale", g("LLAMACPP_ROPE_SCALE")]
        if g("LLAMACPP_YARN_ORIG_CTX") and g("LLAMACPP_YARN_ORIG_CTX") != "0":
            argv += ["--yarn-orig-ctx", g("LLAMACPP_YARN_ORIG_CTX")]
    if g("LLAMACPP_N_PREDICT"):
        argv += ["--predict", g("LLAMACPP_N_PREDICT")]
    mmproj = g("LLAMACPP_MMPROJ")
    if mmproj:
        argv += ["--mmproj", mmproj]
    extra = g("LLAMACPP_EXTRA_ARGS")
    if extra:
        argv += shlex.split(extra)   # model-specific flags (e.g. MTP spec-decode)
    return argv


@dataclasses.dataclass
class NativePlan:
    llama_server: list[str]
    manual_steps: list[str]          # what native mode does NOT orchestrate (best-effort honesty)
    warnings: list[str]

    def as_text(self) -> str:
        lines = ["# Native launch plan (Docker-primary; this path is best-effort)",
                 "", "## 1. Serve the model (native llama.cpp):",
                 "  " + " ".join(shlex.quote(a) for a in self.llama_server), "",
                 "## 2. Manual steps native mode does not manage:"]
        lines += [f"  - {s}" for s in self.manual_steps]
        if self.warnings:
            lines += ["", "## Warnings:"] + [f"  ! {w}" for w in self.warnings]
        return "\n".join(lines) + "\n"


def plan(rc, models_dir: str = "./models") -> NativePlan:
    """Build a native launch plan from a RenderedConfig."""
    warnings: list[str] = []
    if not rc.hardware.has_gpu:
        warnings.append("no GPU detected — llama.cpp will run on CPU (slow for large models)")
    if rc.compose_profiles:
        warnings.append(
            f"media/voice plugins ({', '.join(rc.compose_profiles)}) are Docker-only — "
            "native mode does not launch them")
    manual = [
        "model-gateway (LiteLLM): pip install 'litellm[proxy]' and point it at "
        "http://127.0.0.1:8080 as model 'local-chat'",
        "mcp-gateway: run the MCP gateway pointed at out/mcp-registry.yaml",
        "ops-controller: `ordo serve` (the control plane runs natively as-is)",
        f"agent ({rc.hermes.get('agent', 'hermes')}): start your agent against the gateways",
    ]
    return NativePlan(
        llama_server=llama_server_argv(rc.env, models_dir=models_dir),
        manual_steps=manual, warnings=warnings + rc.warnings)
