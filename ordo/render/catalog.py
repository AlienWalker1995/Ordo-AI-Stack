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

from . import llamacpp_backend
from .hardware import HardwareProfile, compute_cap_tuple, normalize_compute_cap

# Tier ordering, smallest → largest, for "force a tier" and best-fit ranking.
TIER_ORDER = ["cpu", "low", "medium", "high", "ultra"]

# VRAM the sizer keeps free for KV cache + compute buffers + other resident services.
# Deliberately generous — headroom is why chat stays fast.
DEFAULT_VRAM_RESERVE_GB = 4.0

# Where the chat service mounts the models volume: a pinned projector's `file` is rendered as
# LLAMACPP_MMPROJ=<this>/<file>.
CHAT_MODELS_MOUNT = "/models"


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
    backend_image: str | None = None   # override the default llama.cpp image (e.g. a patched build)
    # The lowest NVIDIA compute capability `backend_image` has kernels for ("12.0" for the
    # sm_120-only patched build). Required with a backend_image: the sizer only picks the model on a
    # GPU known to meet it. "" = no requirement (the upstream per-backend images).
    min_compute_cap: str = ""
    # The source needs a Hugging Face token (a gated repo). The fetch hands HF_TOKEN to the download
    # only for such a model, so an ungated download never carries the token.
    gated: bool = False
    # Bytes of the weights file, pinned with its sha256. The preflight disk check sums the files
    # still to fetch; None falls back to the vram_gb / ram_gb estimate.
    size_bytes: int | None = None
    # The vision projector as a downloadable entry of its own, when the catalog pins its source
    # (`mmproj:` as a mapping). A bare `mmproj:` path has no source: it has to be copied in by hand.
    projector: Model | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Model:
        req = d.get("requires", {}) or {}
        mmproj = d.get("mmproj") or None
        projector = None
        if isinstance(mmproj, dict):
            projector = _projector(str(d["id"]), str(d.get("name", d["id"])), mmproj)
            mmproj = f"{CHAT_MODELS_MOUNT}/{projector.file}"
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            backend=str(d.get("backend", "llama.cpp")), file=str(d.get("file", "")),
            source=str(d.get("source", "")), sha256=(d.get("sha256") or None),
            vram_gb=float(req.get("vram_gb", 0)), ram_gb=float(req.get("ram_gb", 0)),
            cpu_ok=bool(req.get("cpu_ok", False)),
            ctx_default=int(d.get("ctx_default", 8192)), tier=str(d.get("tier", "low")),
            kv_kb_per_token=(float(d["kv_kb_per_token"]) if d.get("kv_kb_per_token") else None),
            mmproj=mmproj, extra_args=str(d.get("extra_args", "")),
            backend_image=(d.get("backend_image") or None),
            min_compute_cap=normalize_compute_cap(req.get("min_compute_cap")),
            gated=bool(d.get("gated", False)),
            size_bytes=(int(d["size_bytes"]) if d.get("size_bytes") else None),
            projector=projector,
        )

    def _rank(self) -> tuple[int, float]:
        return (TIER_ORDER.index(self.tier) if self.tier in TIER_ORDER else -1, self.vram_gb)


def _projector(model_id: str, model_name: str, spec: dict[str, Any]) -> Model:
    """A pinned `mmproj:` mapping as a downloadable entry, id `<model id>-mmproj`.

    Its `file` is the name it gets in the models volume. Upstream projectors are nearly all named
    mmproj-F16.gguf, so the source's own name would collide between models."""
    missing = [key for key in ("file", "source", "sha256") if not spec.get(key)]
    if missing:
        raise ValueError(f"{model_id}: the mmproj entry must pin {', '.join(missing)} "
                         "(or be a bare path to a projector copied in by hand)")
    return Model.from_dict({
        "id": f"{model_id}-mmproj", "name": f"{model_name}, vision projector", "backend": "llama.cpp",
        "file": spec["file"], "source": spec["source"], "sha256": spec["sha256"],
        "size_bytes": spec.get("size_bytes"), "gated": spec.get("gated", False),
        "requires": {"vram_gb": 0, "ram_gb": 0, "cpu_ok": True}, "tier": "projector",
    })


def compute_blocker(m: Model, hw: HardwareProfile) -> str:
    """Why this host cannot run the special build `m` pins ("" when it can, or m pins none).

    The build targets `m.min_compute_cap` and up, on CUDA only. An NVIDIA GPU whose capability is
    unknown does not qualify: handing an sm_120-only build to an unknown card is how a 48GB L40S
    got an LLM that cannot start."""
    if not m.min_compute_cap:
        return ""
    backend, _notes = llamacpp_backend.select(hw)
    gpu = hw.primary_gpu
    if backend.name != "cuda" or gpu is None:
        return (f"needs its CUDA build {m.backend_image} (compute capability {m.min_compute_cap}+), "
                f"and this host's llama.cpp backend is {backend.name}")
    have = compute_cap_tuple(gpu.compute_cap)
    if have is None:
        return (f"needs compute capability {m.min_compute_cap}+ ({m.backend_image}) and the compute "
                f"capability of {gpu.name} is unknown; declare it as hardware.gpus[].compute_cap "
                "(nvidia-smi --query-gpu=compute_cap --format=csv)")
    if have < compute_cap_tuple(m.min_compute_cap):
        return (f"needs compute capability {m.min_compute_cap}+ ({m.backend_image}); "
                f"{gpu.name} is {gpu.compute_cap}")
    return ""


class Catalog:
    """`models` are the chat models the sizer picks from. `support_models` are the other weights the
    rendered stack reads from the same models-gguf volume (the CPU fallback, the embedder): pinned
    here so `ordo fetch` can provision them, and never candidates for the chat model."""

    def __init__(self, models: list[Model], support_models: list[Model] | None = None):
        self.models = models
        self.support_models = list(support_models or [])
        self._by_id = {m.id: m for m in models}

    @classmethod
    def load(cls, path: str | Path) -> Catalog:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls([Model.from_dict(m) for m in (data.get("models") or [])],
                   [Model.from_dict(m) for m in (data.get("support_models") or [])])

    def get(self, model_id: str) -> Model | None:
        """A chat model by id (support models are not chat models)."""
        return self._by_id.get(model_id)

    def entries(self) -> list[Model]:
        """Every downloadable entry: chat models, then support models, then pinned projectors."""
        projectors = [m.projector for m in self.models + self.support_models if m.projector]
        return self.models + self.support_models + projectors

    @staticmethod
    def files_of(model: Model) -> list[Model]:
        """The entries a model needs in the volume: its weights, then its pinned projector."""
        return [model] + ([model.projector] if model.projector else [])

    def get_entry(self, model_id: str) -> Model | None:
        """Any downloadable entry by id, chat or support."""
        return next((m for m in self.entries() if m.id == model_id), None)

    def by_file(self, file: str) -> Model | None:
        """The entry whose weights file is `file` (the name it has in the models volume)."""
        return next((m for m in self.entries() if m.file == file), None)

    def fits(self, m: Model, hw: HardwareProfile, reserve_gb: float = DEFAULT_VRAM_RESERVE_GB) -> bool:
        return not compute_blocker(m, hw) and self._fits_budget(m, hw, reserve_gb)

    @staticmethod
    def _fits_budget(m: Model, hw: HardwareProfile, reserve_gb: float) -> bool:
        """VRAM (GPU backend) or RAM + cpu_ok (CPU backend), ignoring which build the model needs."""
        backend, _notes = llamacpp_backend.select(hw)
        if backend.accelerated and m.vram_gb > 0:
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
        # Models the budget allows but whose special build cannot run on this GPU. Reported below
        # when one outranks the pick, so a big non-Blackwell card says why it got a smaller model.
        build_blocked = [m for m in self.models
                         if self._fits_budget(m, hw, reserve_gb) and compute_blocker(m, hw)]

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

        chosen = max(candidates, key=Model._rank)
        skipped_by_reason: dict[str, list[str]] = {}
        for m in build_blocked:
            if m._rank() > chosen._rank():
                skipped_by_reason.setdefault(compute_blocker(m, hw), []).append(m.id)
        for reason, ids in skipped_by_reason.items():
            warnings.append(f"skipped {', '.join(ids)}: {reason}")
        return chosen, warnings

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
            blocker = compute_blocker(m, hw)
            if blocker:
                warnings.append(f"'{m.id}' {blocker} (override honored anyway; expect llama.cpp to fail "
                                "to load it)")
            if not self._fits_budget(m, hw, reserve_gb):
                warnings.append(
                    f"'{m.id}' needs ~{m.vram_gb:.0f}GB VRAM but only "
                    f"~{max(hw.primary_vram_gb - reserve_gb, 0):.0f}GB is usable — "
                    "expect CPU-offload/OOM (override honored anyway)"
                )
            return m, warnings
        return self.best_fit(hw, tier, reserve_gb)
