"""Curated model catalog + best-fit selection.

Best-fit reserves VRAM headroom on top of a model's weights so the sizer never picks a model
that fills the card — that exact mistake (28GB weights on a 32GB card) caused the
saturation → RAM-spill → 2.4 tok/s incident. The reserve covers KV cache + compute buffers +
the other resident services (embed, a possibly-idle ComfyUI, etc.).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from .hardware import HardwareProfile

# Tier ordering, smallest → largest, for "force a tier" and best-fit ranking.
TIER_ORDER = ["cpu", "low", "medium", "high", "ultra"]

# VRAM the sizer keeps free for KV cache + compute buffers + other resident services.
# Deliberately generous — headroom is why chat stays fast.
DEFAULT_VRAM_RESERVE_GB = 4.0


@dataclasses.dataclass(frozen=True)
class Model:
    id: str
    name: str
    backend: str
    file: str
    source: str
    sha256: str | None
    vram_gb: float
    ram_gb: float
    cpu_ok: bool
    ctx_default: int
    tier: str
    kv_kb_per_token: float | None = None
    mmproj: str | None = None          # vision projector (multimodal models)
    extra_args: str = ""               # model-specific llama.cpp flags (e.g. MTP spec-decode)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Model":
        req = d.get("requires", {}) or {}
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            backend=str(d.get("backend", "llama.cpp")), file=str(d.get("file", "")),
            source=str(d.get("source", "")), sha256=(d.get("sha256") or None),
            vram_gb=float(req.get("vram_gb", 0)), ram_gb=float(req.get("ram_gb", 0)),
            cpu_ok=bool(req.get("cpu_ok", False)),
            ctx_default=int(d.get("ctx_default", 8192)), tier=str(d.get("tier", "low")),
            kv_kb_per_token=(float(d["kv_kb_per_token"]) if d.get("kv_kb_per_token") else None),
            mmproj=(d.get("mmproj") or None), extra_args=str(d.get("extra_args", "")),
        )

    def _rank(self) -> tuple[int, float]:
        return (TIER_ORDER.index(self.tier) if self.tier in TIER_ORDER else -1, self.vram_gb)


class Catalog:
    def __init__(self, models: list[Model]):
        self.models = models
        self._by_id = {m.id: m for m in models}

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls([Model.from_dict(m) for m in (data.get("models") or [])])

    def get(self, model_id: str) -> Model | None:
        return self._by_id.get(model_id)

    def fits(self, m: Model, hw: HardwareProfile, reserve_gb: float = DEFAULT_VRAM_RESERVE_GB) -> bool:
        if hw.has_gpu and m.vram_gb > 0:
            return m.vram_gb <= (hw.primary_vram_gb - reserve_gb)
        # CPU path: model must support CPU and fit in RAM
        return m.cpu_ok and m.ram_gb <= hw.ram_gb if hw.ram_gb else m.cpu_ok

    def best_fit(
        self, hw: HardwareProfile, tier: str | None = None,
        reserve_gb: float = DEFAULT_VRAM_RESERVE_GB,
    ) -> tuple[Model, list[str]]:
        """Return (chosen model, warnings). Never raises — always yields a runnable choice."""
        warnings: list[str] = []
        candidates = [m for m in self.models if self.fits(m, hw, reserve_gb)]

        if tier and tier != "auto":
            tier_c = [m for m in candidates if m.tier == tier]
            if tier_c:
                candidates = tier_c
            elif tier in TIER_ORDER:
                # requested tier doesn't fit → fall back to what does, and say so
                warnings.append(
                    f"tier '{tier}' does not fit this hardware; falling back to best-fit"
                )

        if not candidates:
            # Nothing fits (tiny hardware) → smallest CPU-capable model as the floor
            cpu_models = sorted((m for m in self.models if m.cpu_ok), key=lambda m: m.vram_gb)
            if not cpu_models:
                raise ValueError("catalog has no CPU-capable model to serve as the floor")
            warnings.append(
                "no model fits within VRAM/RAM budget; using smallest CPU-capable model"
            )
            return cpu_models[0], warnings

        return max(candidates, key=Model._rank), warnings

    def resolve(
        self, hw: HardwareProfile, model_id: str = "auto", tier: str | None = "auto",
        reserve_gb: float = DEFAULT_VRAM_RESERVE_GB,
    ) -> tuple[Model, list[str]]:
        """Top-level selection honoring an explicit model override (warn-but-allow)."""
        if model_id and model_id != "auto":
            m = self.get(model_id)
            if not m:
                raise ValueError(f"model '{model_id}' not in catalog")
            warnings: list[str] = []
            if not self.fits(m, hw, reserve_gb):
                warnings.append(
                    f"'{m.id}' needs ~{m.vram_gb:.0f}GB VRAM but only "
                    f"~{max(hw.primary_vram_gb - reserve_gb, 0):.0f}GB is usable — "
                    "expect CPU-offload/OOM (override honored anyway)"
                )
            return m, warnings
        return self.best_fit(hw, tier, reserve_gb)
