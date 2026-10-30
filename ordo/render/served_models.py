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

from . import engines
from .gpu import GpuClaim
from .models_volume import CHAT_SERVICE, NeededFile, required_model_files
from .stack import expand_env

# ComfyUI-style services load whichever checkpoints a workflow names; every other one loads one model.
_MULTI_MODEL_KINDS = frozenset({"image"})

# `--ctx-size N` (llama.cpp's spelling) in a service's command is its context window.
_CTX_FLAG = "--ctx-size"


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


def _source(service: str, spec: dict, served: Mapping[str, str], doc: dict, env: Mapping[str, str]) -> dict[str, str]:
    if service == CHAT_SERVICE:
        return {"file": env["LLAMACPP_MODEL"]} if env.get("LLAMACPP_MODEL") else {}
    if served["model_env"]:   # a model named in the service's environment (a repo id), not a file
        value = (spec.get("environment") or {}).get(served["model_env"])
        return {"file": str(value)} if value else {}
    loaded = [f for f in required_model_files(doc, env, [service]) if not f.optional]
    return {"file": loaded[0].file} if loaded else {}


def _config(service: str, spec: dict, env: Mapping[str, str]) -> dict[str, Any]:
    if service == CHAT_SERVICE:
        config: dict[str, Any] = {"ctx": _ctx(env.get("LLAMACPP_CTX_SIZE"))}
        if env.get("LLAMACPP_MMPROJ"):
            config["mmproj"] = env["LLAMACPP_MMPROJ"]
        return config
    command = spec.get("command") or []
    args = [expand_env(str(arg), dict(env)) for arg in (command.split() if isinstance(command, str) else command)]
    if _CTX_FLAG in args[:-1]:
        return {"ctx": _ctx(args[args.index(_CTX_FLAG) + 1])}
    return {}


def served_models(doc: dict, env: Mapping[str, str], claims: Sequence[GpuClaim]) -> dict[str, dict[str, Any]]:
    """One record per model the rendered stack serves, keyed by model id: every service whose
    labels declare one (ordo/render/engines.py), so a new engine needs no change here.

    The record shape is the one `/registry/models` has always answered (id, kind, service,
    runtime, source.file, gpu_uuid, enabled, config.ctx/mmproj, est_vram_gb), because the
    dashboard and Hermes read it, plus the engine's device, metrics reader and fallback target.
    A service the render does not run has no record."""
    services = doc.get("services") or {}
    vram_by_service = {c.service: c.vram_gb for c in claims}
    records: dict[str, dict[str, Any]] = {}
    for service, spec in services.items():
        served = engines.read(spec.get("labels"))
        if served is None:
            continue
        records[served["model"]] = {
            "id": served["model"],
            "kind": served["kind"],
            "service": service,
            "runtime": "multi-model" if served["kind"] in _MULTI_MODEL_KINDS else "single-model",
            "source": _source(service, spec, served, doc, env),
            "gpu_uuid": pinned_gpu(spec),
            "enabled": True,
            "config": _config(service, spec, env),
            "est_vram_gb": vram_by_service.get(service, 0.0),
            "device": served["device"],
            "metrics": {"format": served["metrics"], "path": served["metrics_path"]},
            "fallback_for": served["fallback_for"] or None,
        }
    return records


def models_by_gpu(records: Mapping[str, dict[str, Any]]) -> dict[str, list[str]]:
    """gpu uuid -> the ids of the models pinned to it."""
    by_gpu: dict[str, list[str]] = {}
    for model_id, record in records.items():
        if record.get("gpu_uuid"):
            by_gpu.setdefault(record["gpu_uuid"], []).append(model_id)
    return by_gpu
