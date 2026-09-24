"""What the current render serves: every model a service loads, derived from the render alone.

This replaces the runtime model registry (`data/ops-controller/model-registry.json`). That file
was a second record of which GGUF and projector llama.cpp serves, next to `ordo.yaml`; once GPU
assignment went 410 nothing wrote it, so it matched the render only by coincidence. Everything
here is computed from the rendered compose, its `.env` and the declared GPU claims, so it cannot
disagree with what the stack actually starts.

Pure functions, no I/O: ops-controller answers `/model-config`, `/registry/models` and
`/registry/gpus` from them.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .fetch import CHAT_SERVICE, NeededFile, required_model_files
from .gpu import GpuClaim

# The compose services that serve a model, and the id clients address it by. The chat ids are the
# model-gateway's stable aliases (`local-chat`, `local-chat-cpu`, `local-embed`).
SERVED_MODEL_IDS: dict[str, tuple[str, str]] = {
    # service: (model id, kind)
    CHAT_SERVICE: ("local-chat", "chat"),
    "llamacpp-cpu": ("local-chat-cpu", "chat"),
    "llamacpp-embed": ("local-embed", "embedding"),
    "comfyui": ("comfyui", "comfyui"),
    "stt": ("voice-stt", "stt"),
    "tts": ("voice-tts", "tts"),
}

# A service whose model is not a file in the models volume names it in its environment instead.
_MODEL_ENV_KEYS: dict[str, str] = {
    "stt": "WHISPER__MODEL",   # the Hugging Face repo faster-whisper loads (services/voice)
}

# ComfyUI loads whichever checkpoints a workflow names; every other service loads one model.
_MULTI_MODEL_SERVICES = frozenset({"comfyui"})


def model_files(doc: dict, env: Mapping[str, str]) -> list[NeededFile]:
    """Every file in the models volume that a rendered service loads: the chat model and its
    projector, the CPU fallback's model, the embedding model."""
    return required_model_files(doc, env, list(doc.get("services") or {}))


def pinned_gpu(spec: dict) -> str | None:
    """The one GPU uuid a compose service is pinned to, or None (no GPU, or every GPU)."""
    reservations = ((spec.get("deploy") or {}).get("resources") or {}).get("reservations") or {}
    for device in reservations.get("devices") or []:
        ids = device.get("device_ids") or []
        if len(ids) == 1:
            return str(ids[0])
    return None


def _ctx(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _source(service: str, spec: dict, doc: dict, env: Mapping[str, str]) -> dict[str, str]:
    if service == CHAT_SERVICE:
        return {"file": env["LLAMACPP_MODEL"]} if env.get("LLAMACPP_MODEL") else {}
    env_key = _MODEL_ENV_KEYS.get(service)
    if env_key:
        value = (spec.get("environment") or {}).get(env_key)
        return {"file": str(value)} if value else {}
    loaded = [f for f in required_model_files(doc, env, [service]) if not f.optional]
    return {"file": loaded[0].file} if loaded else {}


def _config(service: str, env: Mapping[str, str]) -> dict[str, Any]:
    if service == CHAT_SERVICE:
        config: dict[str, Any] = {"ctx": _ctx(env.get("LLAMACPP_CTX_SIZE"))}
        if env.get("LLAMACPP_MMPROJ"):
            config["mmproj"] = env["LLAMACPP_MMPROJ"]
        return config
    if service == "llamacpp-cpu":
        return {"ctx": _ctx(env.get("LLAMACPP_CPU_CTX"))}
    return {}


def served_models(doc: dict, env: Mapping[str, str], claims: Sequence[GpuClaim]) -> dict[str, dict[str, Any]]:
    """One record per model the rendered stack serves, keyed by model id.

    The record shape is the one `/registry/models` has always answered (id, kind, service,
    runtime, source.file, gpu_uuid, enabled, config.ctx/mmproj, est_vram_gb), because the
    dashboard and Hermes read it. A service the render does not run has no record."""
    services = doc.get("services") or {}
    vram_by_service = {c.service: c.vram_gb for c in claims}
    records: dict[str, dict[str, Any]] = {}
    for service, (model_id, kind) in SERVED_MODEL_IDS.items():
        spec = services.get(service)
        if spec is None:
            continue
        records[model_id] = {
            "id": model_id,
            "kind": kind,
            "service": service,
            "runtime": "multi-model" if service in _MULTI_MODEL_SERVICES else "single-model",
            "source": _source(service, spec, doc, env),
            "gpu_uuid": pinned_gpu(spec),
            "enabled": True,
            "config": _config(service, env),
            "est_vram_gb": vram_by_service.get(service, 0.0),
        }
    return records


def models_by_gpu(records: Mapping[str, dict[str, Any]]) -> dict[str, list[str]]:
    """gpu uuid -> the ids of the models pinned to it."""
    by_gpu: dict[str, list[str]] = {}
    for model_id, record in records.items():
        if record.get("gpu_uuid"):
            by_gpu.setdefault(record["gpu_uuid"], []).append(model_id)
    return by_gpu
