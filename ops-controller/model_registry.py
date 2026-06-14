"""Single source of truth for managed models + their GPU assignment.

Pure logic + file IO (no FastAPI, no docker) so it is unit-testable. ops-controller
mounts this as the registry behind /registry/*; the dashboard and Hermes are equal
clients. The registry is the *intent*; `.env` and overrides/gpu-assignments.yml are
*derived enforcement* (see derive_env / derive_gpu_assignment).
"""
from __future__ import annotations

import json
import os
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
        """Env keys this record implies when enabled. Empty for multi-model (comfyui)."""
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
