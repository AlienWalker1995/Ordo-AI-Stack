"""Single source of truth for managed models + their GPU assignment.

Pure logic + file IO (no FastAPI, no docker) so it is unit-testable. ops-controller
mounts this as the registry behind /registry/*; the dashboard and Hermes are equal
clients. The registry records the *intent*; the rendered compose file is what enforces it
(model files and GPU pins are baked in at `ordo render` time).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["chat", "embedding", "stt", "tts", "comfyui"]
Runtime = Literal["single-model", "multi-model"]


class ModelRecord(BaseModel):
    id: str
    kind: Kind
    service: str
    runtime: Runtime
    source: dict[str, Any] = Field(default_factory=dict)
    gpu_uuid: str | None = None
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    est_vram_gb: float = 0.0
    updated_by: str = "system"
    updated_at: str | None = None


# Force resolution of the module-level Literal aliases (Kind/Runtime) when this
# file is loaded via importlib spec loading (tests + ops-controller's sibling import).
ModelRecord.model_rebuild()


class ModelRegistry:
    def __init__(self, registry_path: Path, env_path: Path):
        self.registry_path = Path(registry_path)
        self.env_path = Path(env_path)

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

    def get(self, model_id: str) -> ModelRecord | None:
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

    def reconcile(self) -> None:
        """Seed the registry from authoritative files. SEED-ONLY semantics: the
        registry is the source of intent, so a record that already exists is left
        untouched (all its fields are operator/registry-owned). Observed file values
        are used ONLY to create records that don't exist yet (first run). Operators
        change models via the registry verbs, never by reconcile clobbering them."""
        env = _parse_env(self.env_path)
        existing = self.list_models()

        def _ctx(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        # Seed tuples: (id, kind, service, model_file, cfg, est_vram_gb)
        # model_file: str or None — used as source["file"]; for non-file services
        # (stt uses a HF repo ID, tts a voice name) the value is stored identically.
        seeds = [
            ("local-chat", "chat", "llamacpp", env.get("LLAMACPP_MODEL"),
             {"ctx": _ctx(env.get("LLAMACPP_CTX_SIZE")), "mmproj": env.get("LLAMACPP_MMPROJ")},
             0.0),
            ("local-embed", "embedding", "llamacpp-embed", env.get("LLAMACPP_EMBED_MODEL"), {}, 0.0),
            ("comfyui", "comfyui", "comfyui", None, {}, 0.0),
            ("voice-stt", "stt", "stt",
             env.get("STT_MODEL", "Systran/faster-whisper-small"), {}, 2.0),
            ("voice-tts", "tts", "tts",
             env.get("TTS_VOICE", "af_bella"), {}, 1.0),
        ]
        for mid, kind, service, model_file, cfg, est_vram in seeds:
            if mid in existing:
                continue  # registry already owns this record — preserve operator intent
            runtime = "multi-model" if kind == "comfyui" else "single-model"
            cfg = {k: v for k, v in cfg.items() if v is not None}
            self.upsert(ModelRecord(
                id=mid, kind=kind, service=service, runtime=runtime,
                source={"file": model_file} if model_file else {},
                gpu_uuid=None,
                enabled=True,
                config=cfg,
                est_vram_gb=est_vram,
                updated_by="reconcile",
            ))


# ---------------------------------------------------------------------------
# Module-level helpers (shared, no registry state needed)
# ---------------------------------------------------------------------------

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


def capacity_check(gpus: dict[str, dict], gpu_uuid: str,
                   enabled_models: list[ModelRecord], candidate_gb: float
                   ) -> tuple[bool, float, float]:
    """Sum est VRAM of enabled models already on gpu_uuid + candidate vs total.
    Returns (fits, used_gb, total_gb)."""
    total = float(gpus.get(gpu_uuid, {}).get("total_gb", 0.0))
    used = sum(m.est_vram_gb for m in enabled_models
               if m.gpu_uuid == gpu_uuid and m.enabled)
    return (used + candidate_gb <= total, used, total)
