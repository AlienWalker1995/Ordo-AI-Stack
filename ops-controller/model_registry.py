"""Single source of truth for managed models + their GPU assignment.

Pure logic + file IO (no FastAPI, no docker) so it is unit-testable. ops-controller
mounts this as the registry behind /registry/*; the dashboard and Hermes are equal
clients. The registry is the *intent*; `.env` and overrides/gpu-assignments.yml are
*derived enforcement* (see derive_env / derive_gpu_assignment).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Kind = Literal["chat", "embedding", "stt", "tts", "comfyui"]
Runtime = Literal["single-model", "multi-model"]


class ModelRecord(BaseModel):
    id: str
    kind: Kind
    service: str
    runtime: Runtime
    source: dict[str, Any] = Field(default_factory=dict)
    gpu_uuid: Optional[str] = None
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    est_vram_gb: float = 0.0
    updated_by: str = "system"
    updated_at: Optional[str] = None


# Force resolution of the module-level Literal aliases (Kind/Runtime) when this
# file is loaded via importlib spec loading (tests + ops-controller's sibling import).
ModelRecord.model_rebuild()


class ModelRegistry:
    def __init__(self, registry_path: Path, env_path: Path, gpu_assignments_path: Path):
        self.registry_path = Path(registry_path)
        self.env_path = Path(env_path)
        self.gpu_assignments_path = Path(gpu_assignments_path)

    def _read(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"version": 1, "models": {}}
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"version": 1, "models": {}}

    def _write(self, data: dict[str, Any]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.registry_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.registry_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def list_models(self) -> dict[str, ModelRecord]:
        raw = self._read().get("models", {})
        return {mid: ModelRecord(**rec) for mid, rec in raw.items()}

    def get(self, model_id: str) -> Optional[ModelRecord]:
        return self.list_models().get(model_id)

    def upsert(self, record: ModelRecord) -> ModelRecord:
        data = self._read()
        data.setdefault("models", {})[record.id] = record.model_dump()
        self._write(data)
        return record

    def delete(self, model_id: str) -> None:
        data = self._read()
        data.get("models", {}).pop(model_id, None)
        self._write(data)

    _MODEL_FILE_ENV = {
        "llamacpp": "LLAMACPP_MODEL",
        "llamacpp-embed": "LLAMACPP_EMBED_MODEL",
    }

    def derive_env(self, record: ModelRecord) -> dict[str, str]:
        """Env keys this record implies when enabled. Empty for multi-model (comfyui) and for any single-model service not in _MODEL_FILE_ENV."""
        if record.runtime != "single-model":
            return {}
        out: dict[str, str] = {}
        key = self._MODEL_FILE_ENV.get(record.service)
        if key and record.source.get("file"):
            out[key] = str(record.source["file"])
        if record.service == "llamacpp":
            cfg = record.config or {}
            if cfg.get("ctx") is not None:
                out["LLAMACPP_CTX_SIZE"] = str(cfg["ctx"])
            if cfg.get("mmproj"):
                out["LLAMACPP_MMPROJ"] = str(cfg["mmproj"])
            if cfg.get("kv_cache_k"):
                out["LLAMACPP_KV_CACHE_TYPE_K"] = str(cfg["kv_cache_k"])
            if cfg.get("kv_cache_v"):
                out["LLAMACPP_KV_CACHE_TYPE_V"] = str(cfg["kv_cache_v"])
        return out

    def derive_gpu_assignment(self, record: ModelRecord) -> tuple[str, Optional[str]]:
        """(service, gpu_uuid) — the pin this record implies. uuid None = unassigned."""
        return (record.service, record.gpu_uuid)

    def reconcile(self) -> None:
        """Seed the registry from authoritative files. SEED-ONLY semantics: the
        registry is the source of intent, so a record that already exists is left
        untouched (all its fields are operator/registry-owned). Observed file values
        are used ONLY to create records that don't exist yet (first run). Operators
        change models via the registry verbs, never by reconcile clobbering them."""
        env = _parse_env(self.env_path)
        pins: dict[str, Optional[str]] = {}
        if self.gpu_assignments_path.exists():
            pins = parse_gpu_assignments_yaml(
                self.gpu_assignments_path.read_text(encoding="utf-8"))
        existing = self.list_models()

        def _ctx(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        seeds = [
            ("local-chat", "chat", "llamacpp", env.get("LLAMACPP_MODEL"),
             {"ctx": _ctx(env.get("LLAMACPP_CTX_SIZE")), "mmproj": env.get("LLAMACPP_MMPROJ")}),
            ("local-embed", "embedding", "llamacpp-embed", env.get("LLAMACPP_EMBED_MODEL"), {}),
            ("comfyui", "comfyui", "comfyui", None, {}),
        ]
        for mid, kind, service, model_file, cfg in seeds:
            if mid in existing:
                continue  # registry already owns this record — preserve operator intent
            runtime = "multi-model" if kind == "comfyui" else "single-model"
            cfg = {k: v for k, v in cfg.items() if v is not None}
            self.upsert(ModelRecord(
                id=mid, kind=kind, service=service, runtime=runtime,
                source={"file": model_file} if model_file else {},
                gpu_uuid=pins.get(service),
                enabled=True,
                config=cfg,
                est_vram_gb=0.0,
                updated_by="reconcile",
            ))


# ---------------------------------------------------------------------------
# Module-level helpers (shared, no registry state needed)
# ---------------------------------------------------------------------------

def parse_gpu_assignments_yaml(text: str) -> dict[str, str]:
    """Parse the fixed-format gpu-assignments.yml into {service: uuid}.
    Mirrors ops-controller/main.py parse_gpu_assignments_yaml but also handles
    single-quoted UUIDs (both ' and " are accepted)."""
    result: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^  (\S+):\s*$", line)
        if m:
            current = m.group(1)
            continue
        m = re.search(r"device_ids:\s*\[['\"]([^'\"]+)['\"]\]", line)
        if m and current:
            result[current] = m.group(1)
            current = None
    return result


def _parse_env(path: Path) -> dict[str, str]:
    """Read a dotenv file, return {KEY: VALUE} for simple KEY=VALUE lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            v = value.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            result[key.strip()] = v
    return result


def render_gpu_assignments_yaml(assignments: dict[str, str]) -> str:
    """Canonical emitter — both CUDA_VISIBLE_DEVICES (WSL2-effective) and device_ids
    (native-Linux). Replaces the duplicated format_gpu_assignments / render_gpu_assignments."""
    lines = [
        "# Machine-local GPU pins. Generated from the model registry — edit via the dashboard GPU view.",
        "# Both layers (CUDA_VISIBLE_DEVICES + device_ids) are required (see detect_hardware.py / WSL2).",
        "services:",
    ]
    for service, uuid in assignments.items():
        if not uuid:
            continue
        lines += [
            f"  {service}:",
            "    environment:",
            f"      - CUDA_VISIBLE_DEVICES={uuid}",
            f"      - NVIDIA_VISIBLE_DEVICES={uuid}",
            "    deploy:",
            "      resources:",
            "        reservations:",
            "          devices:",
            "            - driver: nvidia",
            f"              device_ids: ['{uuid}']",
            "              capabilities: ['gpu']",
        ]
    return "\n".join(lines) + "\n"


def capacity_check(gpus: dict[str, dict], gpu_uuid: str,
                   enabled_models: list[ModelRecord], candidate_gb: float
                   ) -> tuple[bool, float, float]:
    """Sum est VRAM of enabled models already on gpu_uuid + candidate vs total.
    Returns (fits, used_gb, total_gb)."""
    total = float(gpus.get(gpu_uuid, {}).get("total_gb", 0.0))
    used = sum(m.est_vram_gb for m in enabled_models
               if m.gpu_uuid == gpu_uuid and m.enabled)
    return (used + candidate_gb <= total, used, total)
